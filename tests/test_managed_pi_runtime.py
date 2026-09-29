from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime import agent_tools, managed_pi_runtime as managed_runtime
from rag_ime.agent_tool_ids import (
    CONTROL_TOOL_IDS,
)
from rag_ime.managed_pi_runtime import (
    ACCEPTANCE_SCHEMA_VERSION,
    LIFECYCLE_SCHEMA_VERSION,
    LOCK_NAME,
    MANIFEST_NAME,
    POINTER_NAME,
    POINTER_SCHEMA_VERSION,
    RETENTION_SCHEMA_VERSION,
    ManagedPiRuntimeError,
    apply_managed_pi_runtime_retention_plan,
    build_managed_pi_runtime_manifest,
    build_managed_pi_runtime_retention_plan,
    discover_managed_pi_runtime,
    install_managed_pi_runtime,
    read_managed_pi_runtime_acceptance_report,
    read_managed_pi_runtime_retention_plan,
    rollback_managed_pi_runtime,
    snapshot_managed_pi_runtime,
    snapshot_managed_pi_runtime_payload,
    write_managed_pi_runtime_manifest,
    write_managed_pi_runtime_retention_report,
)
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.runtime import PiRuntimeHostManager


class ManagedPiRuntimeTests(unittest.TestCase):
    def test_tool_catalog_accepts_current_product_and_rejects_invalid_entries(self) -> None:
        self.assertEqual(managed_runtime._validate_tools(CONTROL_TOOL_IDS), CONTROL_TOOL_IDS)
        for invalid in (["tool"] * 2, ["invalid-name"], [f"tool_{i}" for i in range(129)]):
            with self.subTest(invalid=invalid), self.assertRaises(ManagedPiRuntimeError):
                managed_runtime._validate_tools(invalid)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-managed-pi-")
        self.root = Path(self.tmp.name)
        self.app_support = self.root / "Application Support" / "RagIme"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_install_and_discover_use_only_verified_managed_paths(self) -> None:
        payload, _ = self._payload("runtime-1")

        installed = self._install_accepted(payload)[0]
        discovered = discover_managed_pi_runtime(self.app_support, expected_pi_version="0.80.7")

        self.assertEqual(installed.runtime_version, "runtime-1")
        self.assertEqual(discovered.runtime_dir, (self.app_support / "PiRuntime" / "runtime-1").resolve())
        self.assertEqual(discovered.executable.name, "cli.js")
        self.assertEqual(Path(discovered.node_executable).name, "node")
        self.assertEqual(discovered.extension_path.name, "rag-ime-control.ts")
        self.assertEqual(discovered.tools, CONTROL_TOOL_IDS)
        self.assertIn("work_documents", discovered.tools)
        self.assertNotIn("workDocuments", discovered.tools)
        self.assertTrue((self.app_support / "PiRuntime" / POINTER_NAME).is_file())

    def test_install_rejects_evaluation_only_runtime_before_touching_app_support(self) -> None:
        payload, manifest = self._payload("runtime-evaluation-only")
        manifest["source"] = {
            **dict(manifest["source"]),
            "evaluationOnly": True,
            "evaluationProviderCompatSha256": "a" * 64,
        }
        write_managed_pi_runtime_manifest(payload / MANIFEST_NAME, manifest)

        with self.assertRaisesRegex(ManagedPiRuntimeError, "evaluation-only"):
            self._install_accepted(payload)[0]

        self.assertFalse((self.app_support / "PiRuntime" / POINTER_NAME).exists())

    def test_read_only_snapshot_verifies_runtime_without_lifecycle_lock(self) -> None:
        payload, _ = self._payload("runtime-read-only")
        installed = self._install_accepted(payload)[0]

        with patch(
            "rag_ime.managed_pi_runtime._managed_runtime_lock",
            side_effect=AssertionError("read-only snapshot opened lifecycle lock"),
        ):
            snapshot = snapshot_managed_pi_runtime(
                self.app_support,
                expected_pi_version="0.80.7",
            )

        self.assertEqual(snapshot.runtime_version, installed.runtime_version)
        self.assertEqual(snapshot.manifest_sha256, installed.manifest_sha256)
        self.assertEqual(snapshot.executable, installed.executable)

    def test_direct_payload_snapshot_verifies_without_installing_or_pointer(self) -> None:
        payload, _ = self._payload("runtime-direct-payload")

        snapshot = snapshot_managed_pi_runtime_payload(
            payload,
            expected_pi_version="0.80.7",
        )

        self.assertEqual("runtime-direct-payload", snapshot.runtime_version)
        self.assertEqual(payload.resolve(), snapshot.runtime_dir)
        self.assertFalse((self.app_support / "PiRuntime" / POINTER_NAME).exists())

        snapshot.executable.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ManagedPiRuntimeError,
            "size mismatch|digest mismatch",
        ):
            snapshot_managed_pi_runtime_payload(payload)

    def test_read_only_snapshot_still_rejects_runtime_tampering(self) -> None:
        payload, _ = self._payload("runtime-read-only-tampered")
        installed = self._install_accepted(payload)[0]
        installed.executable.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ManagedPiRuntimeError,
            "size mismatch|digest mismatch",
        ):
            snapshot_managed_pi_runtime(self.app_support)

    def test_managed_default_reaches_every_coordinator_native_projection(self) -> None:
        """The managed Pi manifest must expose every native coding projection.

        `workspace_patch` remains a gateway-only R2 approval flow.  This derives
        only the hidden coordinator tools which declare a native Pi projection,
        so adding a gateway tool or changing its approval risk cannot silently
        widen the managed runtime's tool surface.
        """
        payload, manifest = self._payload("runtime-native-projections")

        coordinator_native_targets = {
            str(spec["id"])
            for spec in agent_tools._TOOL_SPECS
            if spec.get("modelVisible") is False
            and tuple(spec.get("sessionModes") or ()) == ("coordinator",)
            and agent_tools._RUNTIME_TOOL_PROJECTIONS.get(str(spec["id"]))
        }

        self.assertEqual(
            coordinator_native_targets,
            {
                "workspace_list",
                "workspace_read",
                "workspace_search",
                "workspace_edit",
                "workspace_write",
                "workspace_shell",
            },
        )
        self.assertNotIn("workspace_patch", coordinator_native_targets)
        self.assertTrue(
            coordinator_native_targets.issubset(set(manifest["tools"])),
            "managed Pi defaults must reach every coordinator-only native projection",
        )

    def test_discovery_rejects_pointer_and_runtime_file_tampering(self) -> None:
        payload, _ = self._payload("runtime-1")
        installed = self._install_accepted(payload)[0]
        installed.executable.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(ManagedPiRuntimeError, "size mismatch|digest mismatch"):
            discover_managed_pi_runtime(self.app_support)

        payload_two, _ = self._payload("runtime-2")
        self._install_accepted(payload_two)[0]
        pointer_path = self.app_support / "PiRuntime" / POINTER_NAME
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["manifestSha256"] = "0" * 64
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
        with self.assertRaisesRegex(ManagedPiRuntimeError, "digest does not match"):
            discover_managed_pi_runtime(self.app_support)

    def test_manifest_path_traversal_and_symlinks_fail_closed(self) -> None:
        payload, manifest = self._payload("runtime-unsafe")
        manifest["piEntrypoint"] = "../outside.js"
        write_managed_pi_runtime_manifest(payload / MANIFEST_NAME, manifest)
        with self.assertRaisesRegex(ManagedPiRuntimeError, "unsafe managed Pi runtime path"):
            self._install_accepted(payload)[0]

        symlink_payload = self.root / "payload-symlink"
        (symlink_payload / "bin").mkdir(parents=True)
        outside = self.root / "outside-node"
        outside.write_text("node\n", encoding="utf-8")
        (symlink_payload / "bin" / "node").symlink_to(outside)
        with self.assertRaisesRegex(ManagedPiRuntimeError, "symlink"):
            build_managed_pi_runtime_manifest(
                symlink_payload,
                runtime_version="runtime-symlink",
                pi_version="0.80.7",
                launch_kind="node",
                pi_entrypoint="bin/node",
                node_entrypoint="bin/node",
                extension_entrypoint="bin/node",
                source_repository="local/pi",
                source_commit="test",
                source_package="@earendil-works/pi-coding-agent",
            )

    def test_atomic_activation_retains_previous_version_for_rollback(self) -> None:
        first, _ = self._payload("runtime-1")
        second, _ = self._payload("runtime-2")

        self._install_accepted(first)[0]
        self._install_accepted(second)[0]

        runtime_root = self.app_support / "PiRuntime"
        self.assertTrue((runtime_root / "runtime-1" / MANIFEST_NAME).is_file())
        self.assertTrue((runtime_root / "runtime-2" / MANIFEST_NAME).is_file())
        pointer = json.loads((runtime_root / POINTER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(pointer["schemaVersion"], POINTER_SCHEMA_VERSION)
        self.assertEqual(pointer["version"], "runtime-2")
        self.assertEqual(pointer["previousVersion"], "runtime-1")
        self.assertEqual(discover_managed_pi_runtime(self.app_support).runtime_version, "runtime-2")

    def test_retention_dry_run_then_apply_keeps_active_and_immediate_previous(self) -> None:
        for version in ("runtime-1", "runtime-2", "runtime-3"):
            payload, _ = self._payload(version, protocol_version="2")
            self._install_accepted(payload)

        runtime_root = self.app_support / "PiRuntime"
        pointer_path = runtime_root / POINTER_NAME
        pointer_before = json.loads(pointer_path.read_text(encoding="utf-8"))
        plan = build_managed_pi_runtime_retention_plan(
            self.app_support,
            retain_generations=2,
        )

        self.assertEqual(plan["schemaVersion"], RETENTION_SCHEMA_VERSION)
        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(plan["activeGeneration"]["version"], "runtime-3")
        self.assertEqual(plan["previousGeneration"]["version"], "runtime-2")
        self.assertEqual(plan["plannedRemovalCount"], 1)
        self.assertTrue((runtime_root / "runtime-1").is_dir())
        planned = [
            action
            for action in plan["actions"]
            if action.get("status") == "planned"
        ]
        self.assertEqual([action["version"] for action in planned], ["runtime-1"])

        plan_path = self.root / "retention-plan.json"
        write_managed_pi_runtime_retention_report(plan_path, plan)
        self.assertEqual(plan_path.stat().st_mode & 0o777, 0o600)
        applied = apply_managed_pi_runtime_retention_plan(
            read_managed_pi_runtime_retention_plan(plan_path),
            app_support=self.app_support,
        )

        self.assertEqual(applied["mode"], "apply")
        self.assertEqual(applied["removedGenerationCount"], 1)
        self.assertFalse((runtime_root / "runtime-1").exists())
        self.assertTrue((runtime_root / "runtime-2").is_dir())
        self.assertTrue((runtime_root / "runtime-3").is_dir())
        pointer_after = json.loads(pointer_path.read_text(encoding="utf-8"))
        self.assertEqual(pointer_after["version"], pointer_before["version"])
        self.assertEqual(
            pointer_after["previousVersion"],
            pointer_before["previousVersion"],
        )
        self.assertEqual(
            pointer_after["lifecycle"]["retiredGenerations"],
            [],
        )

    def test_retention_preserves_unverified_entries_and_rejects_changed_plan(self) -> None:
        for version in ("runtime-1", "runtime-2", "runtime-3", "runtime-4"):
            payload, _ = self._payload(version, protocol_version="2")
            self._install_accepted(payload)

        runtime_root = self.app_support / "PiRuntime"
        pointer_path = runtime_root / POINTER_NAME
        pointer_before = json.loads(pointer_path.read_text(encoding="utf-8"))
        unowned = runtime_root / "user-notes"
        unowned.mkdir()
        (unowned / "README.txt").write_text("preserve me\n", encoding="utf-8")
        outside = self.root / "outside-runtime"
        outside.mkdir()
        runtime_link = runtime_root / "runtime-link"
        runtime_link.symlink_to(outside, target_is_directory=True)

        stale_plan = build_managed_pi_runtime_retention_plan(self.app_support)
        (runtime_root / "runtime-1" / "lib" / "pi" / "dist" / "cli.js").write_text(
            "tampered after dry-run\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ManagedPiRuntimeError, "state changed after dry-run"):
            apply_managed_pi_runtime_retention_plan(
                stale_plan,
                app_support=self.app_support,
            )

        self.assertTrue((runtime_root / "runtime-1").is_dir())
        self.assertTrue((runtime_root / "runtime-2").is_dir())
        fresh_plan = build_managed_pi_runtime_retention_plan(self.app_support)
        statuses = {
            Path(str(action["target"])).name: action["status"]
            for action in fresh_plan["actions"]
        }
        self.assertEqual(
            statuses["runtime-1"],
            "preserved_retired_unverified",
        )
        self.assertEqual(statuses["runtime-link"], "preserved_symlink")
        self.assertEqual(statuses["user-notes"], "preserved_unverified")
        self.assertEqual(fresh_plan["plannedRemovalCount"], 1)

        applied = apply_managed_pi_runtime_retention_plan(
            fresh_plan,
            app_support=self.app_support,
        )
        self.assertEqual(applied["removedGenerationCount"], 1)
        self.assertTrue((runtime_root / "runtime-1").is_dir())
        self.assertFalse((runtime_root / "runtime-2").exists())
        self.assertTrue((runtime_root / "runtime-3").is_dir())
        self.assertTrue((runtime_root / "runtime-4").is_dir())
        self.assertTrue(runtime_link.is_symlink())
        self.assertTrue(unowned.is_dir())
        pointer_after = json.loads(pointer_path.read_text(encoding="utf-8"))
        self.assertEqual(pointer_after["version"], pointer_before["version"])
        self.assertEqual(
            pointer_after["previousVersion"],
            pointer_before["previousVersion"],
        )
        self.assertEqual(
            [
                entry["version"]
                for entry in pointer_after["lifecycle"]["retiredGenerations"]
            ],
            ["runtime-1"],
        )

    def test_retention_without_verified_predecessor_preserves_old_generations(self) -> None:
        for version in ("runtime-1", "runtime-2"):
            payload, _ = self._payload(version)
            self._install_accepted(payload)[0]

        runtime_root = self.app_support / "PiRuntime"
        pointer_path = runtime_root / POINTER_NAME
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer.pop("lifecycle", None)
        pointer.pop("previousVersion")
        pointer.pop("previousManifestSha256")
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
        staged_payload, _ = self._payload("runtime-legacy-stage")
        install_managed_pi_runtime(
            staged_payload,
            self.app_support,
            activate=False,
        )

        plan = build_managed_pi_runtime_retention_plan(self.app_support)

        self.assertEqual(plan["plannedRemovalCount"], 0)
        self.assertIn("no accepted lifecycle", plan["retentionBlockedReason"])
        self.assertTrue((runtime_root / "runtime-1").is_dir())
        self.assertTrue((runtime_root / "runtime-2").is_dir())
        self.assertTrue((runtime_root / "runtime-legacy-stage").is_dir())
        self.assertEqual(
            {
                action.get("version"): action.get("status")
                for action in plan["actions"]
                if action.get("version")
            }["runtime-legacy-stage"],
            "preserved_staged",
        )

    def test_no_activate_generation_is_preserved_as_staged(self) -> None:
        for version in ("runtime-1", "runtime-2", "runtime-3"):
            payload, _ = self._payload(version, protocol_version="2")
            self._install_accepted(payload)
        staged_payload, _ = self._payload(
            "runtime-staged",
            protocol_version="2",
        )
        staged = install_managed_pi_runtime(
            staged_payload,
            self.app_support,
            activate=False,
        )

        plan = build_managed_pi_runtime_retention_plan(self.app_support)
        statuses = {
            str(action.get("version") or ""): action["status"]
            for action in plan["actions"]
            if action.get("version")
        }

        self.assertEqual(statuses["runtime-staged"], "preserved_staged")
        self.assertEqual(plan["plannedRemovalCount"], 1)
        applied = apply_managed_pi_runtime_retention_plan(
            plan,
            app_support=self.app_support,
        )
        self.assertTrue(applied["ok"])
        self.assertTrue(staged.runtime_dir.is_dir())
        self.assertEqual(
            discover_managed_pi_runtime(self.app_support).runtime_version,
            "runtime-3",
        )

    def test_failed_acceptance_keeps_last_accepted_generation(self) -> None:
        first_payload, _ = self._payload("runtime-1", protocol_version="2")
        first, _first_receipt = self._install_accepted(first_payload)
        second_payload, _ = self._payload("runtime-2", protocol_version="2")
        staged = install_managed_pi_runtime(
            second_payload,
            self.app_support,
            activate=False,
        )
        rejected = {
            **self._acceptance(staged),
            "status": "failed",
        }

        with self.assertRaisesRegex(
            ManagedPiRuntimeError,
            "acceptance receipt did not pass",
        ):
            install_managed_pi_runtime(
                second_payload,
                self.app_support,
                acceptance=rejected,
            )

        active = discover_managed_pi_runtime(self.app_support)
        pointer = json.loads(
            (active.runtime_root / POINTER_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(active.runtime_version, first.runtime_version)
        self.assertEqual(
            pointer["lifecycle"]["acceptedGeneration"]["version"],
            first.runtime_version,
        )
        self.assertTrue(staged.runtime_dir.is_dir())
        plan = build_managed_pi_runtime_retention_plan(self.app_support)
        self.assertEqual(plan["plannedRemovalCount"], 0)

    def test_acceptance_requires_real_prompt_steer_abort_and_context_proofs(self) -> None:
        payload, _ = self._payload("runtime-claims", protocol_version="2")
        staged = install_managed_pi_runtime(
            payload,
            self.app_support,
            activate=False,
        )
        receipt = self._acceptance(staged)
        receipt.pop("debugContextAvailable")

        with self.assertRaisesRegex(
            ManagedPiRuntimeError,
            "Provider context",
        ):
            install_managed_pi_runtime(
                payload,
                self.app_support,
                acceptance=receipt,
            )

        wrong_revision = self._acceptance(staged)
        wrong_revision["steerDelivery"] = "followUp"
        with self.assertRaisesRegex(
            ManagedPiRuntimeError,
            "steer delivery",
        ):
            install_managed_pi_runtime(
                payload,
                self.app_support,
                acceptance=wrong_revision,
            )

    def test_barrier_serializes_retention_and_concurrent_reactivation(self) -> None:
        payloads: dict[str, Path] = {}
        receipts: dict[str, dict[str, object]] = {}
        for version in ("runtime-1", "runtime-2", "runtime-3"):
            payload, _ = self._payload(version, protocol_version="2")
            payloads[version] = payload
            _installation, receipt = self._install_accepted(payload)
            receipts[version] = receipt
        plan = build_managed_pi_runtime_retention_plan(self.app_support)
        entered = threading.Event()
        release = threading.Event()
        retention_result: list[dict[str, object]] = []
        activation_result: list[ManagedPiRuntimeError | None] = []
        original_quarantine = managed_runtime._quarantine_retention_candidate

        def blocking_quarantine(source: Path, destination: Path) -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            original_quarantine(source, destination)

        def run_retention() -> None:
            retention_result.append(
                apply_managed_pi_runtime_retention_plan(
                    plan,
                    app_support=self.app_support,
                )
            )

        def reactivate_retired() -> None:
            try:
                install_managed_pi_runtime(
                    payloads["runtime-1"],
                    self.app_support,
                    acceptance=receipts["runtime-1"],
                )
            except ManagedPiRuntimeError as exc:
                activation_result.append(exc)
            else:
                activation_result.append(None)

        with patch.object(
            managed_runtime,
            "_quarantine_retention_candidate",
            side_effect=blocking_quarantine,
        ):
            retention_thread = threading.Thread(target=run_retention)
            retention_thread.start()
            self.assertTrue(entered.wait(timeout=5))
            activation_thread = threading.Thread(target=reactivate_retired)
            activation_thread.start()
            time.sleep(0.1)
            self.assertTrue(
                activation_thread.is_alive(),
                "activation must wait for the retention lifecycle lock",
            )
            pointer_during_barrier = json.loads(
                (
                    self.app_support / "PiRuntime" / POINTER_NAME
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(pointer_during_barrier["version"], "runtime-3")
            release.set()
            retention_thread.join(timeout=5)
            activation_thread.join(timeout=5)

        self.assertFalse(retention_thread.is_alive())
        self.assertFalse(activation_thread.is_alive())
        self.assertTrue(retention_result[0]["ok"])
        self.assertEqual(activation_result, [None])
        active = discover_managed_pi_runtime(self.app_support)
        self.assertEqual(active.runtime_version, "runtime-1")
        self.assertTrue(active.runtime_dir.is_dir())

    def test_second_quarantine_failure_rolls_back_with_typed_receipts(self) -> None:
        for version in (
            "runtime-1",
            "runtime-2",
            "runtime-3",
            "runtime-4",
            "runtime-5",
        ):
            payload, _ = self._payload(version, protocol_version="2")
            self._install_accepted(payload)
        runtime_root = self.app_support / "PiRuntime"
        pointer_before = (runtime_root / POINTER_NAME).read_bytes()
        plan = build_managed_pi_runtime_retention_plan(self.app_support)
        self.assertEqual(plan["plannedRemovalCount"], 3)
        original_quarantine = managed_runtime._quarantine_retention_candidate
        calls = 0

        def fail_second(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second quarantine failure")
            original_quarantine(source, destination)

        with patch.object(
            managed_runtime,
            "_quarantine_retention_candidate",
            side_effect=fail_second,
        ):
            report = apply_managed_pi_runtime_retention_plan(
                plan,
                app_support=self.app_support,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "rolled_back")
        self.assertEqual(report["removedGenerationCount"], 0)
        status_by_version = {
            action["version"]: action["status"]
            for action in report["actions"]
            if action.get("kind") == "remove_generation"
        }
        self.assertEqual(
            status_by_version,
            {
                "runtime-1": "rolled_back",
                "runtime-2": "failed",
                "runtime-3": "not_attempted",
            },
        )
        for version in ("runtime-1", "runtime-2", "runtime-3"):
            self.assertTrue((runtime_root / version).is_dir())
        self.assertEqual((runtime_root / POINTER_NAME).read_bytes(), pointer_before)

    def test_pointer_write_failure_restores_exact_prior_pointer(self) -> None:
        for version in ("runtime-1", "runtime-2", "runtime-3"):
            payload, _ = self._payload(version, protocol_version="2")
            self._install_accepted(payload)
        runtime_root = self.app_support / "PiRuntime"
        pointer_path = runtime_root / POINTER_NAME
        pointer_before = pointer_path.read_bytes()
        plan = build_managed_pi_runtime_retention_plan(self.app_support)
        original_write = managed_runtime._write_runtime_pointer
        calls = 0

        def fail_after_replacing_pointer(
            root: Path,
            payload: dict[str, object],
        ) -> None:
            nonlocal calls
            calls += 1
            original_write(root, payload)
            if calls == 1:
                raise OSError("injected directory fsync failure")

        with patch.object(
            managed_runtime,
            "_write_runtime_pointer",
            side_effect=fail_after_replacing_pointer,
        ):
            report = apply_managed_pi_runtime_retention_plan(
                plan,
                app_support=self.app_support,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "rolled_back")
        self.assertEqual(calls, 2)
        self.assertEqual(pointer_path.read_bytes(), pointer_before)
        self.assertTrue((runtime_root / "runtime-1").is_dir())
        self.assertFalse((runtime_root / ".retention-quarantine").exists())

    def test_cleanup_pending_is_retried_from_durable_quarantine_receipt(self) -> None:
        for version in ("runtime-1", "runtime-2", "runtime-3"):
            payload, _ = self._payload(version, protocol_version="2")
            self._install_accepted(payload)
        runtime_root = self.app_support / "PiRuntime"
        plan = build_managed_pi_runtime_retention_plan(self.app_support)
        original_rmtree = managed_runtime.shutil.rmtree

        def fail_quarantine_cleanup(
            path: str | Path,
            *args: object,
            **kwargs: object,
        ) -> None:
            target = Path(path)
            if (
                ".retention-quarantine" in target.parts
                and target.name == "runtime-1"
            ):
                raise OSError("injected quarantine cleanup failure")
            original_rmtree(path, *args, **kwargs)

        with patch.object(
            managed_runtime.shutil,
            "rmtree",
            side_effect=fail_quarantine_cleanup,
        ):
            first = apply_managed_pi_runtime_retention_plan(
                plan,
                app_support=self.app_support,
            )

        self.assertFalse(first["ok"])
        self.assertEqual(first["state"], "cleanup_pending")
        self.assertEqual(first["cleanupPendingCount"], 1)
        quarantine_root = runtime_root / ".retention-quarantine"
        receipt_paths = list(
            quarantine_root.glob("*/retention-batch.json")
        )
        self.assertEqual(len(receipt_paths), 1)
        self.assertEqual(
            stat.S_IMODE(receipt_paths[0].stat().st_mode),
            0o600,
        )

        retry_plan = build_managed_pi_runtime_retention_plan(self.app_support)
        self.assertEqual(retry_plan["plannedRemovalCount"], 0)
        self.assertEqual(retry_plan["plannedCleanupCount"], 1)
        cleanup_action = next(
            action
            for action in retry_plan["actions"]
            if action.get("status") == "planned_cleanup"
        )
        self.assertEqual(
            cleanup_action["kind"],
            "cleanup_quarantine_generation",
        )

        retried = apply_managed_pi_runtime_retention_plan(
            retry_plan,
            app_support=self.app_support,
        )
        self.assertTrue(retried["ok"])
        self.assertEqual(retried["state"], "cleanup_applied")
        self.assertEqual(retried["cleanedQuarantineCount"], 1)
        self.assertFalse(quarantine_root.exists())

    def test_rollback_restores_retired_generation_and_updates_lineage(self) -> None:
        for version in ("runtime-1", "runtime-2", "runtime-3"):
            payload, _ = self._payload(version, protocol_version="2")
            self._install_accepted(payload)

        restored = rollback_managed_pi_runtime(
            self.app_support,
            runtime_version="runtime-1",
        )
        pointer = json.loads(
            (restored.runtime_root / POINTER_NAME).read_text(encoding="utf-8")
        )

        self.assertEqual(restored.runtime_version, "runtime-1")
        self.assertEqual(pointer["previousVersion"], "runtime-3")
        self.assertEqual(
            pointer["lifecycle"]["previousAcceptedGeneration"]["version"],
            "runtime-3",
        )
        self.assertEqual(
            [
                entry["version"]
                for entry in pointer["lifecycle"]["retiredGenerations"]
            ],
            ["runtime-2"],
        )
        self.assertTrue((restored.runtime_root / "runtime-3").is_dir())

    def test_rollback_retry_returns_the_already_active_generation(self) -> None:
        for version in ("runtime-1", "runtime-2", "runtime-3"):
            payload, _ = self._payload(version, protocol_version="2")
            self._install_accepted(payload)

        first = rollback_managed_pi_runtime(
            self.app_support,
            runtime_version="runtime-1",
        )
        second = rollback_managed_pi_runtime(
            self.app_support,
            runtime_version="runtime-1",
        )

        self.assertEqual(first.runtime_version, "runtime-1")
        self.assertEqual(second.runtime_version, "runtime-1")
        self.assertEqual(first.manifest_sha256, second.manifest_sha256)

    def test_lifecycle_lock_is_mode_0600_regular_file(self) -> None:
        payload, _ = self._payload("runtime-1")
        self._install_accepted(payload)[0]
        lock_path = self.app_support / LOCK_NAME
        lock_stat = lock_path.stat(follow_symlinks=False)

        self.assertFalse(lock_path.is_symlink())
        self.assertTrue(stat.S_ISREG(lock_stat.st_mode))
        self.assertEqual(stat.S_IMODE(lock_stat.st_mode), 0o600)

    def test_acceptance_report_must_be_private_regular_file(self) -> None:
        report = self.root / "acceptance.json"
        report.write_text("{}\n", encoding="utf-8")
        report.chmod(0o644)
        with self.assertRaisesRegex(ManagedPiRuntimeError, "mode 0600"):
            read_managed_pi_runtime_acceptance_report(report)

        report.chmod(0o600)
        self.assertEqual(
            read_managed_pi_runtime_acceptance_report(report),
            {},
        )
        link = self.root / "acceptance-link.json"
        link.symlink_to(report)
        with self.assertRaisesRegex(ManagedPiRuntimeError, "real file"):
            read_managed_pi_runtime_acceptance_report(link)

    def test_protocol_v2_manifest_is_discovered_by_the_product_runtime(self) -> None:
        payload, _ = self._payload("runtime-v2", protocol_version="2")
        self._install_accepted(payload)

        discovered = discover_managed_pi_runtime(self.app_support, expected_pi_version="0.80.7")
        with patch.dict(
            os.environ,
            {
                "RAG_IME_APP_SUPPORT_DIR": str(self.app_support),
                "RAG_IME_PI_ENABLED": "true",
            },
            clear=True,
        ):
            config = PiRuntimeConfig.from_environment()

        self.assertEqual(discovered.protocol_version, "2")
        self.assertEqual(
            discovered.runtime_methods,
            managed_runtime.REQUIRED_SESSION_RUNTIME_METHODS,
        )
        self.assertEqual(config.protocol_version, "2")

    def test_protocol_v2_activation_requires_an_acceptance_receipt(self) -> None:
        payload, _ = self._payload("runtime-v2-receipt-required", protocol_version="2")

        with self.assertRaisesRegex(ManagedPiRuntimeError, "acceptance receipt"):
            install_managed_pi_runtime(payload, self.app_support)

    def test_retired_protocol_cannot_build_install_or_be_selected_for_execution(self) -> None:
        with self.assertRaisesRegex(ManagedPiRuntimeError, "retired"):
            self._payload("retired-build", protocol_version="1")

        payload, manifest = self._payload("historical-v1")
        manifest["runtimeProtocolVersion"] = "1"
        manifest.pop("runtimeMethods", None)
        write_managed_pi_runtime_manifest(payload / MANIFEST_NAME, manifest)
        with self.assertRaisesRegex(ManagedPiRuntimeError, "retired"):
            install_managed_pi_runtime(payload, self.app_support)
        self.assertFalse(self.app_support.exists())
        with self.assertRaisesRegex(ManagedPiRuntimeError, "retired"):
            snapshot_managed_pi_runtime_payload(payload)

    def test_explicit_legacy_execution_configuration_is_rejected(self) -> None:
        executable = self.root / "old-pi"
        executable.write_text("#!/bin/sh\nexit 0\n")
        with patch.dict(os.environ, {
            "RAG_IME_PI_EXECUTABLE": str(executable),
            "RAG_IME_PI_PROTOCOL_VERSION": "1",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "retired"):
                PiRuntimeConfig.from_environment()

    def test_protocol_v2_manifest_without_complete_session_methods_fails_closed(self) -> None:
        payload, manifest = self._payload("runtime-v2-incomplete", protocol_version="2")
        manifest["runtimeMethods"] = ["session.open"]

        with self.assertRaisesRegex(
            ManagedPiRuntimeError,
            "omits required Session runtime methods",
        ):
            write_managed_pi_runtime_manifest(payload / MANIFEST_NAME, manifest)

    def test_protocol_v2_manifest_accepts_current_runtime_method_surface_with_a_bound(self) -> None:
        payload, manifest = self._payload("runtime-v2-current-methods", protocol_version="2")
        current_methods = (
            *managed_runtime.REQUIRED_SESSION_RUNTIME_METHODS,
            *(f"runtime.capability_{index}" for index in range(31)),
        )
        self.assertEqual(len(current_methods), 37)
        manifest["runtimeMethods"] = list(current_methods)

        write_managed_pi_runtime_manifest(payload / MANIFEST_NAME, manifest)

        manifest["runtimeMethods"] = [
            *managed_runtime.REQUIRED_SESSION_RUNTIME_METHODS,
            *(f"runtime.excess_{index}" for index in range(59)),
        ]
        with self.assertRaisesRegex(
            ManagedPiRuntimeError,
            "method list is invalid",
        ):
            write_managed_pi_runtime_manifest(payload / MANIFEST_NAME, manifest)

    def test_pi_runtime_config_discovers_managed_install_without_path_fallback(self) -> None:
        payload, manifest = self._payload("runtime-1", pi_version="0.84.2")
        installed = self._install_accepted(payload)[0]
        with patch.dict(
            os.environ,
            {
                "RAG_IME_APP_SUPPORT_DIR": str(self.app_support),
                "RAG_IME_PI_ENABLED": "true",
            },
            clear=True,
        ):
            config = PiRuntimeConfig.from_environment()

        self.assertEqual(config.executable, installed.executable)
        self.assertEqual(config.node_executable, installed.node_executable)
        self.assertEqual(config.extension_path, installed.extension_path)
        self.assertEqual(config.pi_version, manifest["piVersion"])
        self.assertEqual(config.installation_error, "")
        command = config.launch_host_command()
        self.assertEqual(command, [installed.node_executable, str(installed.executable)])

    def test_explicit_development_executable_overrides_managed_install(self) -> None:
        payload, _ = self._payload("runtime-1")
        self._install_accepted(payload)[0]
        developer_pi = self.root / "developer-pi.js"
        developer_extension = self.root / "developer-extension.ts"
        developer_pi.write_text("console.log('dev')\n", encoding="utf-8")
        developer_extension.write_text("export default function () {}\n", encoding="utf-8")
        with patch.dict(
            os.environ,
            {
                "RAG_IME_APP_SUPPORT_DIR": str(self.app_support),
                "RAG_IME_PI_EXECUTABLE": str(developer_pi),
                "RAG_IME_PI_NODE": "/dev/node-for-test",
                "RAG_IME_PI_EXTENSION": str(developer_extension),
            },
            clear=True,
        ):
            config = PiRuntimeConfig.from_environment()

        self.assertEqual(config.executable, developer_pi)
        self.assertEqual(config.extension_path, developer_extension)
        self.assertEqual(config.node_executable, "/dev/node-for-test")

    def test_missing_or_invalid_installation_is_visible_but_does_not_use_global_pi(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "/a/path/that/might/contain/global/pi",
                "RAG_IME_APP_SUPPORT_DIR": str(self.app_support),
                "RAG_IME_PI_ENABLED": "true",
            },
            clear=True,
        ):
            config = PiRuntimeConfig.from_environment()
        self.assertIsNone(config.executable)
        self.assertIn("pointer is missing", config.installation_error)

        class _NoSessions:
            db_path = self.root / "unopened.sqlite"

        class _NoEvents:
            pass

        status = PiRuntimeHostManager(config=config, sessions=_NoSessions(), events=_NoEvents()).runtime_status()
        self.assertEqual(status["status"], "not_installed")
        self.assertIn("pointer is missing", status["lastError"])

    def test_required_pi_version_mismatch_fails_closed(self) -> None:
        payload, _ = self._payload("runtime-1")
        self._install_accepted(payload)[0]
        with self.assertRaisesRegex(ManagedPiRuntimeError, "does not match required"):
            discover_managed_pi_runtime(self.app_support, expected_pi_version="0.81.0")

    def _acceptance(
        self,
        installation: managed_runtime.ManagedPiRuntimeInstallation,
    ) -> dict[str, object]:
        manifest = json.loads(
            (installation.runtime_dir / MANIFEST_NAME).read_text(
                encoding="utf-8"
            )
        )
        source = manifest["source"]
        return {
            "schemaVersion": ACCEPTANCE_SCHEMA_VERSION,
            "status": "passed_not_installed",
            "productionEnabled": False,
            "runtimeVersion": installation.runtime_version,
            "manifestSha256": installation.manifest_sha256,
            "sourceCommit": source["commit"],
            "protocolVersion": installation.protocol_version,
            "verifiedMethods": [
                "session.open",
                "session.prompt",
                "session.steer",
                "session.debug.context",
                "session.abort",
                "session.snapshot",
            ],
            "promptDelivery": "prompt",
            "steerDelivery": "steer",
            "abortAcknowledged": True,
            "debugContextAvailable": True,
            "terminalSessionIdle": True,
        }

    def _install_accepted(
        self,
        payload: Path,
    ) -> tuple[
        managed_runtime.ManagedPiRuntimeInstallation,
        dict[str, object],
    ]:
        staged = install_managed_pi_runtime(
            payload,
            self.app_support,
            activate=False,
        )
        receipt = self._acceptance(staged)
        installed = install_managed_pi_runtime(
            payload,
            self.app_support,
            acceptance=receipt,
        )
        pointer = json.loads(
            (installed.runtime_root / POINTER_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            pointer["lifecycle"]["schemaVersion"],
            LIFECYCLE_SCHEMA_VERSION,
        )
        return installed, receipt

    def _payload(
        self,
        runtime_version: str,
        *,
        protocol_version: str = "2",
        pi_version: str = "0.80.7",
    ) -> tuple[Path, dict[str, object]]:
        payload = self.root / f"payload-{runtime_version}"
        node = payload / "bin" / "node"
        executable = payload / "lib" / "pi" / "dist" / "cli.js"
        extension = payload / "extensions" / "rag-ime-control.ts"
        skill = (
            payload
            / "runtime-host"
            / "skills"
            / "orchestrate-session"
            / "SKILL.md"
        )
        node.parent.mkdir(parents=True)
        executable.parent.mkdir(parents=True)
        extension.parent.mkdir(parents=True)
        skill.parent.mkdir(parents=True)
        node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        node.chmod(0o755)
        executable.write_text(f"console.log('{runtime_version}')\n", encoding="utf-8")
        extension.write_text("export default function () {}\n", encoding="utf-8")
        skill.write_text(
            "---\n"
            "name: orchestrate-session\n"
            "---\n"
            "Delegate bounded Session work.\n",
            encoding="utf-8",
        )
        protocol_v2 = protocol_version == "2"
        manifest = build_managed_pi_runtime_manifest(
            payload,
            runtime_version=runtime_version,
            pi_version=pi_version,
            launch_kind="node",
            pi_entrypoint="lib/pi/dist/cli.js",
            node_entrypoint="bin/node",
            extension_entrypoint="extensions/rag-ime-control.ts",
            source_repository="local/pi",
            source_commit=hashlib.sha256(runtime_version.encode("utf-8")).hexdigest()[:12],
            source_package="@earendil-works/pi-coding-agent",
            protocol_version=protocol_version,
            runtime_methods=(
                managed_runtime.REQUIRED_SESSION_RUNTIME_METHODS
                if protocol_v2
                else ()
            ),
            source_contract_sha256="a" * 64 if protocol_v2 else "",
            handlers_commit="b" * 40 if protocol_v2 else "",
        )
        write_managed_pi_runtime_manifest(payload / MANIFEST_NAME, manifest)
        return payload, manifest


if __name__ == "__main__":
    unittest.main()
