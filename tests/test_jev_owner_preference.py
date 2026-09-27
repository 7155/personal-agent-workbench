"""A task-specific owner preference never weakens execution eligibility."""

import json
from dataclasses import replace
from pathlib import Path

from rag_ime.jev_tasks.candidates import OwnerPreference, build_candidates
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import GraphError, canonical
from tests.test_jev_host_application import JevHostFixture, choose_execution


class JevOwnerPreferenceTests(JevHostFixture):
    def setUp(self):
        super().setUp()
        self.created = self.create()
        self.snapshot_ = self.snapshot(self.created)
        self.observation = self.app.observe(self.snapshot_, None)
        self.task = self.snapshot_.task(self.created["workItemId"])
        self.other = next(
            actor.participant_id for actor in self.observation.executors
            if actor.participant_id != self.task.owner_id
        )

    def preference(self):
        return OwnerPreference(
            owner_id=self.task.owner_id,
            participant_id=self.other,
            task_revision=self.task.revision,
            basis="write task: eligible implementer role is a closer fit than coordinator",
        )

    def prepare_write_task(self, *, lock_owner=False, target_read_only=False):
        for participant in self.room["participants"]:
            self.service.sessions.set_runtime_policy(
                participant["sessionId"], mode="coordinator",
                tool_profile_version="control-center-v1",
                execution_mode=("read_only" if target_read_only and participant["id"] == self.other
                                else "workspace_managed"),
                workspace_roots=[self.tmp.name], allowed_tools=None,
            )
        specification = {"writeTargets": [str(Path(self.tmp.name) / "result.txt")]}
        if lock_owner:
            specification["ownerParticipantId"] = self.task.owner_id
        with self.app.ledger.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO agent_jev_task_requirements VALUES(?,?,?)",
                (self.task.id, self.created["graphId"], canonical(specification)),
            )
        self.observation = self.app.observe(self.snapshot_, None)

    def actions(self, *, executors=None, eligible_pairs=None, executions=None,
                preferences=None, snapshot=None):
        result = build_candidates(
            snapshot or self.snapshot_, event_id="owner-preference-check",
            executions=executions or self.observation.executions,
            executors=executors or self.observation.executors,
            eligible_pairs=eligible_pairs if eligible_pairs is not None else self.observation.eligible_pairs,
            manifests=self.observation.manifests,
            owner_preferences=preferences,
        )
        return result.actions

    def test_proven_better_owner_is_offered_before_dispatch_without_equal_churn(self):
        actions = self.actions(preferences={self.task.id: self.preference()})
        self.assertEqual([action.operation for action in actions], ["reassign", "claim_dispatch"])
        self.assertEqual(actions[0].arguments()["targetParticipantId"], self.other)
        self.assertIn("eligible implementer role", actions[0].arguments()["reason"])
        self.assertEqual([action.operation for action in self.actions()], ["claim_dispatch"])

    def test_preference_cannot_bypass_lock_eligibility_or_busy_target(self):
        preference = {self.task.id: self.preference()}
        locked_pairs = frozenset({(self.task.id, self.task.owner_id)})
        self.assertEqual([a.operation for a in self.actions(
            preferences=preference, eligible_pairs=locked_pairs)], ["claim_dispatch"])
        busy = tuple(replace(actor, available=False) if actor.participant_id == self.other
                     else actor for actor in self.observation.executors)
        self.assertEqual([a.operation for a in self.actions(
            preferences=preference, executors=busy)], ["claim_dispatch"])

    def test_stale_owner_or_task_revision_cannot_cause_reassignment(self):
        preference = self.preference()
        for stale in (replace(preference, owner_id="stale-owner"),
                      replace(preference, task_revision=self.task.revision + 1)):
            with self.subTest(stale=stale):
                self.assertEqual([a.operation for a in self.actions(
                    preferences={self.task.id: stale})], ["claim_dispatch"])
        reassigned = replace(self.task, owner_id=self.other)
        snapshot = replace(self.snapshot_, tasks=(reassigned,))
        fact = self.observation.executions[self.task.id]
        executions = {self.task.id: replace(fact, owner_id=self.other)}
        self.assertEqual([a.operation for a in self.actions(
            snapshot=snapshot, executions=executions,
            preferences={self.task.id: preference})], ["claim_dispatch"])

    def test_unknown_and_unreconciled_attempt_do_not_offer_a_new_owner(self):
        fact = self.observation.executions[self.task.id]
        unknown = {**self.observation.executions, self.task.id: replace(fact, status="unknown")}
        self.assertEqual(self.actions(
            preferences={self.task.id: self.preference()}, executions=unknown), ())
        attempted_task = replace(self.task, accepted_turn_id="accepted-turn")
        attempted_snapshot = replace(self.snapshot_, tasks=(attempted_task,))
        unreconciled = {self.task.id: replace(
            fact, status="drained", dispatch_id="dispatch", session_id="executor-session",
            accepted_turn_id="accepted-turn", effects_reconciled=False,
        )}
        self.assertEqual(self.actions(
            snapshot=attempted_snapshot, preferences={self.task.id: self.preference()},
            executions=unreconciled), ())
        running = {self.task.id: replace(unreconciled[self.task.id], status="running")}
        self.assertEqual([a.operation for a in self.actions(
            snapshot=attempted_snapshot, preferences={self.task.id: self.preference()},
            executions=running)], ["wait"])

    def test_invalid_preference_reference_is_rejected(self):
        with self.assertRaises(GraphError):
            self.actions(preferences={"other-task": self.preference()})

    def test_host_observes_explicit_write_role_preference_and_driver_offers_it(self):
        self.prepare_write_task()
        preference = self.observation.owner_preferences[self.task.id]
        self.assertEqual((preference.owner_id, preference.participant_id,
                          preference.task_revision),
                         (self.task.owner_id, self.other, self.task.revision))
        observed_actions = []

        def decide(state, questions):
            observed_actions.extend(json.loads(state)["actions"])
            return choose_execution(state, questions)

        self.app.driver.controller.decider = JevChoices(decide)
        self.app.tick(limit=1)
        self.assertEqual([action["operation"] for action in observed_actions],
                         ["reassign", "claim_dispatch"])
        reassigned = self.snapshot(self.created)
        self.assertEqual(reassigned.task(self.task.id).owner_id, self.other)
        self.assertEqual(self.app.observe(reassigned, None).owner_preferences, {})
        self.prompt.assert_not_called()
        self.app.tick(limit=1)  # The committed assignment wakes the next owner event.
        admitted = self.snapshot(self.created).task(self.task.id)
        effect = self.app.effects.get(admitted.accepted_turn_id)
        self.assertEqual(effect["state"], "accepted")
        self.assertEqual(effect["request"]["ownerId"], self.other)
        self.assertEqual(self.prompt.call_args.args[0], next(
            participant["sessionId"] for participant in self.room["participants"]
            if participant["id"] == self.other))
        self.assertEqual(self.prompt.call_count, 1)

    def test_host_without_write_target_has_no_preference(self):
        self.assertEqual(self.observation.owner_preferences, {})

    def test_host_respects_explicit_owner_lock(self):
        self.prepare_write_task(lock_owner=True)
        self.assertEqual(self.observation.owner_preferences, {})

    def test_host_respects_target_write_ineligibility(self):
        self.prepare_write_task(target_read_only=True)
        self.assertEqual(self.observation.owner_preferences, {})

    def test_host_keeps_current_implementer(self):
        self.prepare_write_task()
        self.service.rooms.update_participant_role(
            self.room["id"], self.task.owner_id, "implementer")
        self.observation = self.app.observe(self.snapshot_, None)
        self.assertEqual(self.observation.owner_preferences, {})
