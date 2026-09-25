"""
Outbound-URL guard for the hosted (web) build of triageQ.

The desktop app fetches whatever URL its one user types, which is fine: the
user and the machine are the same person. A public server is different. Every
visitor-supplied link — a batch row's url, a landing page's citation_pdf_url,
a redirect, an OpenAI-compatible base URL — becomes a request made FROM the
server, and without a check a visitor could aim it at addresses only the
server can reach: the cloud metadata service (169.254.169.254, which hands out
instance credentials), localhost admin ports, or the private network.

check_url() refuses any URL whose host resolves to a non-public address. It is
applied to every hop of a redirect chain (see pdf_resolver._get), because an
innocent-looking public URL can 302 to 169.254.169.254.

Known limit: DNS is resolved here and again by the HTTP client, so a hostile
DNS server could answer differently the second time ("DNS rebinding"). That is
an accepted gap for a reference deployment; DEPLOY.md also adds a host
firewall rule that drops container traffic to the metadata address, which
covers the worst case independently of this check.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedURL(ValueError):
    """Raised for a URL the server must not fetch. The message is safe to show."""


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_url(url: str) -> None:
    """Raise BlockedURL unless url is http(s) and every address its host
    resolves to is publicly routable."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"only http(s) links are allowed (got {parts.scheme or 'none'})")
    host = parts.hostname
    if not host:
        raise BlockedURL("link has no host")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise BlockedURL(f"could not resolve host {host}")
    for info in infos:
        ip = info[4][0]
        if not _is_public(ip):
            raise BlockedURL(f"{host} resolves to a private or reserved address")
