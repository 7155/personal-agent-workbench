"""Old membership removal cannot mutate current Jev responsibility bindings."""
from tests.test_jev_host_application import JevHostFixture


class JevParticipantGuardTests(JevHostFixture):
    def test_pending_responsibility_cannot_be_released_through_old_membership_path(self):
        created = self.create()
        before = self.snapshot(created)
        owner = before.task(created["workItemId"]).owner_id
        replacement = next(p["id"] for p in self.room["participants"] if p["id"] != owner)
        with self.assertRaisesRegex(ValueError, "reclaimed by its graph"):
            self.service.room_work.release_for_participant(self.room["id"], owner,
                replacement_participant_id=replacement)
        self.assertEqual(self.snapshot(created).fingerprint, before.fingerprint)
        self.prompt.assert_not_called()

    def test_terminal_root_keeps_ordinary_membership_cleanup_available(self):
        created = self.create()
        owner = self.snapshot(created).task(created["workItemId"]).owner_id
        replacement = next(p["id"] for p in self.room["participants"] if p["id"] != owner)
        self.app.stop(self.room["id"], created["rootId"])
        self.service.room_work.release_for_participant(self.room["id"], owner,
            replacement_participant_id=replacement)
        self.assertTrue(self.app.lifecycle.policy(created["graphId"])["stopped"])
