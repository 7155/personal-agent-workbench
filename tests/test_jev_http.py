"""Real HTTP routing/SSE over canonical stores; provider/Pi seam is explicit."""

import json
from pathlib import Path
import threading
from urllib.parse import quote
from urllib.request import Request, urlopen

from tests import test_jev_host_application as host
from rag_ime.debug_server import (
    DebugImeService,
    DebugRequestHandler,
    DebugServerConfig,
    QuietThreadingHTTPServer,
)


class JevHttpTests(host.JevHostFixture):
    def test_http_command_projection_and_sse_reconnect_do_not_repeat_dispatch(self):
        app = DebugImeService(
            DebugServerConfig(
                db_path=self.service.db_path,
                agent_service=self.service,
                seed_if_empty=False,
                memory_projection_worker_enabled=False,
                rime_user_dir=Path(self.tmp.name) / "Rime",
                rime_lexicon_backup_root=Path(self.tmp.name) / "backup",
            )
        )
        self.addCleanup(app.close)

        class Handler(DebugRequestHandler):
            def log_message(self, *args):
                pass

        Handler.service = app
        Handler.static_dir = Path("debug")
        server = QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/api/agent/rooms/" + quote(
            self.room["id"], safe=""
        )
        payload = {
            "action": "create",
            "message": "HTTP synthetic check",
            "clientMessageId": "http-1",
            "strategy": "direct",
            "modelRouting": "participant",
        }

        def create():
            with urlopen(
                Request(
                    base + "/jev",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=10,
            ) as response:
                return json.load(response)

        created = create()
        self.assertTrue(create()["idempotentReplay"])
        self.app.tick()
        calls = len(self.calls)
        for _ in range(2):
            with urlopen(
                base + "/jev?graphId=" + quote(created["graphId"], safe=""), timeout=10
            ) as response:
                view = json.load(response)
            self.assertEqual(view["graphId"], created["graphId"])
            self.assertEqual(len(self.calls), calls)
        with urlopen(base + "/events", timeout=10) as response:
            self.assertIn("text/event-stream", response.headers["Content-Type"])
            lines = []
            while len(lines) < 15:
                line = response.readline().decode()
                lines.append(line)
                if "jev_updated" in line:
                    break
            self.assertIn(created["rootId"], "".join(lines))
        self.assertEqual(len(self.calls), calls)
