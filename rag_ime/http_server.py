"""HTTP listeners whose numeric loopback binds do not depend on reverse DNS."""

from __future__ import annotations

import ipaddress
import socket
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackThreadingHTTPServer(ThreadingHTTPServer):
    """Retain HTTPServer metadata without resolving the known loopback name."""

    def server_bind(self) -> None:
        # TCPServer owns socket options, bind and the actual ephemeral port.
        # HTTPServer's extra getfqdn() can block even for 127.0.0.1 on macOS.
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = "localhost" if ipaddress.ip_address(host).is_loopback else socket.getfqdn(host)
        self.server_port = port
