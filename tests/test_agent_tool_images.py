import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from rag_ime.agent_media import AgentMediaStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway


class AgentToolImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWooAAAAASUVORK5CYII=")
        self.media = AgentMediaStore(Path(self.tmp.name) / "state.sqlite")
        self.media.initialize()
        sessions = AgentSessionStore(self.media.db_path)
        self.viewer = sessions.create(title="image reader")["id"]
        self.other = sessions.create(title="other reader")["id"]
        self.gateway = ControlToolGateway.__new__(ControlToolGateway)
        self.gateway.collaboration = SimpleNamespace(
            media=self.media, read_media_resource=self.media.read)
        self.capture = {"commandId": "bcmd_test", "status": "completed", "result": {
            "snapshotId": "snap_" + "a" * 32, "imagePath": "/api/browser/snapshots/snap_" + "a" * 32 + "/image"}}
        self.gateway.browser_control = Mock()
        self.gateway.browser_control.submit_command.return_value = self.capture
        self.gateway.browser_control.snapshot_image.return_value = ("image/png", self.raw)

    def test_browser_capture_delivers_pixels_and_owned_reusable_read_ref(self):
        result = self.gateway._browser("screenshot", {"_sessionId": self.viewer})
        image = result["_modelImages"][0]
        self.assertEqual(base64.b64decode(image["data"]), self.raw)
        self.assertEqual(image["mimeType"], "image/png")
        reread = self.gateway._read_internal_resource(self.viewer, {"resourceRef": result["imageReadRef"]})
        self.assertEqual(reread["_modelImages"], result["_modelImages"])
        self.assertEqual(result["imageMedia"]["originReceiptId"], "bcmd_test")
        with self.assertRaises(KeyError):
            self.gateway._read_internal_resource(self.other, {"resourceRef": result["imageReadRef"]})

    def test_failed_screenshot_has_no_invented_pixels(self):
        self.capture["status"] = "failed"
        result = self.gateway._browser("screenshot", {"_sessionId": self.viewer})
        self.assertNotIn("_modelImages", result)
        self.gateway.browser_control.snapshot_image.assert_not_called()

    def test_room_capture_has_shared_ref_and_session_transcript_image(self):
        with sqlite3.connect(self.media.db_path) as conn:
            conn.execute("INSERT INTO agent_rooms(id,title,routing_policy,status,room_file,created_at_ms,updated_at_ms,last_event_sequence) VALUES ('room:images','Images','manual_mentions','active','',1,1,0)")
        self.gateway.collaboration._active_room_dispatch_context = lambda session_id: {
            "roomId": "room:images", "rootId": "root:test", "dispatchId": "dispatch:test", "generation": 1}
        result = self.gateway._browser("screenshot", {"_sessionId": self.viewer})
        media_id = result["imageReadRef"].removeprefix("media://")
        self.assertEqual(self.media.read(media_id, room_id="room:images")[1], self.raw)
        with self.assertRaises(KeyError):
            self.media.read(media_id, room_id="room:another")
        self.assertTrue(self.media.resolve_pi_image(self.viewer, "image/png", result["_modelImages"][0]["data"]))
        self.gateway.collaboration.read_media_resource = lambda media_id, session_id: self.media.read(media_id, room_id="room:images")
        reread = self.gateway._read_internal_resource(self.other, {"resourceRef": result["imageReadRef"]})
        self.assertTrue(self.media.resolve_pi_image(self.other, "image/png", reread["_modelImages"][0]["data"]))

    def test_text_media_keeps_lossless_line_pagination(self):
        media = self.media.import_bytes(session_id=self.viewer, data=b"one\ntwo\n",
                                       mime_type="text/plain", file_name="evidence.txt")
        result = self.gateway._read_internal_resource(self.viewer, {
            "resourceRef": "media://" + media["mediaId"], "lineLimit": 1})
        self.assertEqual(result["content"], "one\n")
        self.assertEqual(result["nextLineOffset"], 2)
        self.assertNotIn("_modelImages", result)
