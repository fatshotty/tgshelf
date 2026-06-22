"""In-memory registry of rclone rc endpoints (self-registration).

The rclone client declares its own rc URL at mount time via the `X-Tgshelf-RC`
header (carried on every WebDAV request); a middleware authorises it and calls
`RcRegistry.touch`. The control-plane bridge reads `live_endpoints()` and pushes
`vfs/forget` to each. No rc endpoint lives in the config (user decision); multiple
mounts can register at once, each kept alive by its own traffic and dropped after
`ttl` seconds of silence.

`is_rc_authorized` is the anti-SSRF gate: tgshelf will POST to a client-supplied
URL, so registration requires the shared `register_token` AND the declared rc host
must be an IP literal that is either the request's own source IP or inside the
`allowed_rc_networks` allowlist. Hostnames are rejected (no DNS, no rebinding).

Both pieces are pure (an injectable monotonic clock) so they unit-test without a
network or a real loop.
"""

from __future__ import annotations

import ipaddress
import time
from typing import Callable, Sequence
from urllib.parse import urlsplit


class RcRegistry:
    def __init__(self, *, ttl: float, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, float] = {}  # rc_url -> last_seen (monotonic)

    def touch(self, rc_url: str) -> None:
        """Register/refresh an rc endpoint as alive now."""
        self._entries[rc_url] = self._clock()

    def live_endpoints(self) -> list[str]:
        """rc URLs seen within the TTL; expired ones are pruned lazily here."""
        now = self._clock()
        live = [url for url, seen in self._entries.items() if now - seen <= self._ttl]
        self._entries = {url: self._entries[url] for url in live}
        return live


def is_rc_authorized(
    rc_url: str | None,
    token: str | None,
    *,
    register_token: str,
    allowed_networks: Sequence[str] = (),
    source_ip: str | None = None,
) -> bool:
    """Whether a client may register `rc_url` as a push target.

    False unless: self-registration is enabled (`register_token` set) and the
    presented `token` matches; the URL is http(s) with an IP-literal host; and that
    IP is the request's `source_ip` or falls inside one of `allowed_networks`.
    """
    if not register_token or token != register_token or not rc_url:
        return False
    parts = urlsplit(rc_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        host_ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return False  # hostnames are not accepted (can't be validated safely)

    if source_ip is not None:
        try:
            if host_ip == ipaddress.ip_address(source_ip):
                return True
        except ValueError:
            pass
    for cidr in allowed_networks:
        try:
            if host_ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False
