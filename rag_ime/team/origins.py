"""Canonical browser HTTP origins used by the two team gateways."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


def canonical_http_origin(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError('Configure one HTTP(S) origin without whitespace or credentials')
    parsed = urlsplit(value.rstrip('/'))
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or '\\' in value):
        raise ValueError('Configure one HTTP(S) origin without paths or credentials')
    hostname = parsed.hostname
    if '%' in hostname:
        raise ValueError('Use a canonical hostname without percent encoding')
    if ':' in hostname:
        hostname = '[' + ipaddress.IPv6Address(hostname).compressed + ']'
    else:
        hostname = hostname.encode('idna').decode('ascii').lower()
        if len(hostname) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in hostname.split('.')):
            raise ValueError('HTTP(S) origin hostname is invalid')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('HTTP(S) origin port is invalid')
    suffix = '' if port in {None, 80 if parsed.scheme == 'http' else 443} else f':{port}'
    return parsed.scheme + '://' + hostname + suffix
