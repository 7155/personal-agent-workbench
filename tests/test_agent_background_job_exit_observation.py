from __future__ import annotations

import subprocess
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import rag_ime.agent_background_jobs as job_module
from rag_ime.agent_background_jobs import AgentBackgroundJobService, _LiveJob
from rag_ime.agent_workspace import SpawnedWorkspaceCommand, WorkspaceHarness


class BackgroundJobExitObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.live = _LiveJob(
            launched=None,
            log_path=Path("unused.log"),
            max_run_seconds=60,
            job_id="bg_exit_observation",
            pid=424242,
            process_group_id=424242,
            process_birth_token="original-birth",
            started_at_ms=1_000,
        )
        # Bound a broken monitor without sleeping or launching a real process.
        self.live.detach_requested = Mock()
        self.live.detach_requested.wait.side_effect = [
            False, AssertionError("an exited leader must stop the monitor"),
        ]
        self.live.detach_requested.is_set.return_value = False
        self.service = AgentBackgroundJobService.__new__(AgentBackgroundJobService)
        self.service._lock = threading.RLock()
        self.service._live = {self.live.job_id: self.live}
        self.service._ownership = SimpleNamespace(guard=lambda _job_id: nullcontext())
        self.service._poll_control = Mock()
        self.service._read_exit_status = Mock(return_value=None)
        self.service._drain_raw_output = Mock()
        self.service._persist_live_progress = Mock()
        self.service._request_timeout = Mock()
        self.service._commit_result = Mock(return_value=("background_job_failed", {}))
        self.service._publish = Mock()
        self.service._cleanup_runtime_files = Mock()
        self.service._finalize_without_process = Mock()

    def _monitor_zombie(self, *, exit_codes, clock_values):
        self.service._read_exit_status.side_effect = exit_codes
        with (
            patch.object(job_module, "_process_identity", return_value=(424242, "original-birth")),
            patch.object(job_module.os, "getpgrp", return_value=1),
            patch.object(
                job_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="Zs\n"),
            ),
            patch.object(job_module, "_signal_process_group") as signal_group,
            patch.object(job_module, "_now_ms", return_value=100_000),
            patch.object(job_module.time, "monotonic", side_effect=clock_values),
            patch.object(job_module.time, "sleep"),
        ):
            self.service._monitor_resumable(self.live.job_id, self.live)
        return signal_group

    def test_unreaped_leader_without_receipt_settles_and_cleans_its_group(self) -> None:
        signal_group = self._monitor_zombie(
            exit_codes=[None, None], clock_values=[10.0, 10.5, 11.0],
        )

        self.service._commit_result.assert_called_once_with(
            self.live.job_id, self.live, exit_code=None, timed_out=False,
        )
        self.service._request_timeout.assert_not_called()
        self.service._finalize_without_process.assert_not_called()
        signal_group.assert_called_once_with(424242)
        self.service._cleanup_runtime_files.assert_called_once_with(self.live)
        self.assertEqual(self.service._live, {})

    def test_exited_leader_keeps_grace_for_a_durable_exit_receipt(self) -> None:
        signal_group = self._monitor_zombie(
            exit_codes=[None, 0], clock_values=[10.0, 10.5],
        )

        self.service._commit_result.assert_called_once_with(
            self.live.job_id, self.live, exit_code=0, timed_out=False,
        )
        self.service._request_timeout.assert_not_called()
        self.service._finalize_without_process.assert_not_called()
        signal_group.assert_called_once_with(424242)

    def test_legacy_zombie_preserves_group_cleanup_before_orphaning(self) -> None:
        self.service._row_for_job = Mock(return_value={"status": "running"})
        with (
            patch.object(job_module, "_process_identity", return_value=(424242, "original-birth")),
            patch.object(job_module.os, "getpgrp", return_value=1),
            patch.object(
                job_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="Zs\n"),
            ),
            patch.object(job_module, "_signal_process_group") as signal_group,
        ):
            self.service._monitor_legacy_recovery(self.live.job_id, self.live)

        signal_group.assert_called_once_with(424242)
        self.assertEqual(
            self.service._finalize_without_process.call_args.kwargs["status"],
            "orphaned",
        )
        self.assertEqual(self.service._live, {})

    def test_owned_child_exit_observation_never_reaps_the_leader(self) -> None:
        process = Mock(spec=subprocess.Popen)
        self.live.launched = SpawnedWorkspaceCommand(process=process)
        with (
            patch.object(WorkspaceHarness, "_exit_observed_without_reaping", return_value=True) as observe,
            patch.object(job_module.subprocess, "run") as run,
        ):
            self.assertTrue(self.service._leader_exit_observed(self.live))

        observe.assert_called_once_with(process)
        run.assert_not_called()
        process.poll.assert_not_called()
        process.wait.assert_not_called()

    def test_live_stopped_and_unknown_recovered_states_do_not_prove_exit(self) -> None:
        for state in ("R", "S", "D", "T", "t", "I", "", "Zs S"):
            with self.subTest(state=state):
                with patch.object(
                    job_module.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=state),
                ):
                    self.assertFalse(self.service._leader_exit_observed(self.live))
        with patch.object(
            job_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="Zs"),
        ):
            self.assertFalse(self.service._leader_exit_observed(self.live))
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("ps", 1)):
            with self.subTest(error=type(error).__name__):
                with patch.object(job_module.subprocess, "run", side_effect=error):
                    self.assertFalse(self.service._leader_exit_observed(self.live))


if __name__ == "__main__":
    unittest.main()
