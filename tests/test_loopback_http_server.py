from __future__ import annotations

import tempfile
import threading
import unittest
import socket
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from rag_ime.agent_lab.app_runtime import create_server
from rag_ime.debug_server import QuietThreadingHTTPServer
from tests.test_agent_lab_apps import write_app


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass


class LoopbackHTTPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = write_app(Path(self.temp.name))
        self.environment = patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def factories(self):
        return (
            ("gateway", lambda host: QuietThreadingHTTPServer((host, 0), _Handler)),
            ("standalone", lambda host: create_server(self.root, host, 0)),
        )

    def test_loopback_startup_does_not_call_failing_reverse_dns(self):
        for name, create in self.factories():
            for host in ("127.0.0.1", "localhost"):
                with self.subTest(server=name, host=host), patch(
                    "socket.getfqdn", side_effect=OSError("reverse DNS unavailable")
                ) as reverse_dns:
                    with create(host) as server:
                        self.assertEqual(server.server_name, "localhost")
                        self.assertEqual(server.server_port, server.socket.getsockname()[1])
                        self.assertGreater(server.server_port, 0)
                        self.assertEqual(server.server_address[0], "127.0.0.1")
                    self.assertEqual(server.socket.fileno(), -1)
                    reverse_dns.assert_not_called()

    def test_loopback_startup_does_not_wait_for_blocked_reverse_dns(self):
        for name, create in self.factories():
            with self.subTest(server=name):
                release = threading.Event()
                ready = threading.Event()
                servers = []
                errors = []

                def blocked_lookup(_host, release=release):
                    release.wait(3)
                    return "localhost"

                def start_server(create=create, servers=servers, ready=ready, errors=errors):
                    try:
                        servers.append(create("127.0.0.1"))
                        ready.set()
                    except BaseException as error:
                        errors.append(error)

                worker = threading.Thread(target=start_server)
                with patch("socket.getfqdn", side_effect=blocked_lookup) as reverse_dns:
                    try:
                        worker.start()
                        self.assertTrue(ready.wait(1), "loopback startup waited for reverse DNS")
                        self.assertFalse(errors)
                        reverse_dns.assert_not_called()
                    finally:
                        release.set()
                        worker.join(3)
                        for server in servers:
                            server.server_close()
                self.assertFalse(worker.is_alive())

    def test_loopback_serves_http_and_closes_listener_and_worker(self):
        for name, create in self.factories():
            with self.subTest(server=name), patch(
                "socket.getfqdn", side_effect=OSError("reverse DNS unavailable")
            ) as reverse_dns:
                server = create("127.0.0.1")
                worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
                try:
                    worker.start()
                    with build_opener(ProxyHandler({})).open(
                        f"http://127.0.0.1:{server.server_port}/", timeout=3
                    ) as response:
                        self.assertEqual(response.status, 200)
                        self.assertTrue(response.read())
                    reverse_dns.assert_not_called()
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(server.socket.fileno(), -1)

    def test_non_loopback_keeps_resolved_server_name_and_bound_port(self):
        for name, create in self.factories():
            with self.subTest(server=name), patch(
                "socket.getfqdn", return_value="resolved-host.example"
            ) as reverse_dns:
                with create("0.0.0.0") as server:
                    reverse_dns.assert_called_once_with("0.0.0.0")
                    self.assertEqual(server.server_name, "resolved-host.example")
                    self.assertEqual(server.server_port, server.socket.getsockname()[1])
                    self.assertEqual(server.server_address[0], "0.0.0.0")

    def test_bound_address_drives_ipv4_and_ipv6_name_without_second_bind(self):
        from rag_ime.agent_lab.app_runtime import _AppHTTPServer
        from rag_ime.http_server import LoopbackThreadingHTTPServer

        for server_type in (QuietThreadingHTTPServer, _AppHTTPServer, LoopbackThreadingHTTPServer):
            for address, loopback in (
                (("127.1.2.3", 32123), True),
                (("::1", 32123, 0, 0), True),
                (("192.0.2.1", 32123), False),
                (("::", 32123, 0, 0), False),
                (("2001:db8::1", 32123, 0, 0), False),
            ):
                with self.subTest(server=server_type.__name__, address=address):
                    server = object.__new__(server_type)
                    server.server_address = ("requested-host.example", 0)

                    def bind(bound_server, bound_address=address):
                        bound_server.server_address = bound_address

                    with patch.object(TCPServer, "server_bind", autospec=True, side_effect=bind) as bind_socket, patch(
                        "socket.getfqdn", return_value="resolved-host.example"
                    ) as reverse_dns:
                        server.server_bind()
                    bind_socket.assert_called_once_with(server)
                    self.assertEqual(server.server_port, 32123)
                    self.assertEqual(server.server_address, address)
                    if loopback:
                        self.assertEqual(server.server_name, "localhost")
                        reverse_dns.assert_not_called()
                    else:
                        self.assertEqual(server.server_name, "resolved-host.example")
                        reverse_dns.assert_called_once_with(address[0])

    def test_ipv6_loopback_keeps_existing_address_family_support(self):
        from rag_ime.agent_lab.app_runtime import _AppHTTPServer
        from rag_ime.http_server import LoopbackThreadingHTTPServer

        if not socket.has_ipv6:
            self.skipTest("IPv6 is unavailable")
        for server_type in (QuietThreadingHTTPServer, _AppHTTPServer, LoopbackThreadingHTTPServer):
            with self.subTest(server=server_type.__name__):
                class IPv6Server(server_type):
                    address_family = socket.AF_INET6

                with patch("socket.getfqdn", side_effect=OSError("reverse DNS unavailable")) as reverse_dns:
                    try:
                        server = IPv6Server(("::1", 0), _Handler)
                    except OSError as error:
                        if error.errno in {49, 97, 99}:  # No configured IPv6 loopback.
                            self.skipTest(str(error))
                        raise
                    with server:
                        self.assertEqual(server.server_name, "localhost")
                        self.assertEqual(server.server_address[0], "::1")
                        self.assertEqual(server.server_port, server.socket.getsockname()[1])
                    reverse_dns.assert_not_called()
                    self.assertEqual(server.socket.fileno(), -1)

    def test_non_loopback_lookup_failure_still_closes_bound_socket(self):
        from rag_ime.agent_lab.app_runtime import _AppHTTPServer
        from rag_ime.http_server import LoopbackThreadingHTTPServer

        for server_type in (QuietThreadingHTTPServer, _AppHTTPServer, LoopbackThreadingHTTPServer):
            with self.subTest(server=server_type.__name__), patch(
                "socket.getfqdn", side_effect=OSError("reverse DNS unavailable")
            ), patch.object(server_type, "server_close", autospec=True, side_effect=server_type.server_close) as close:
                with self.assertRaisesRegex(OSError, "reverse DNS unavailable"):
                    server_type(("0.0.0.0", 0), _Handler)
                close.assert_called_once()
                self.assertEqual(close.call_args.args[0].socket.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
