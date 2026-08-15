"""
Guarded fetcher — SSRF-safe HTTPS client for tenant-supplied URLs.

Every tenant-registered custom-MCP-server URL (ADR-006 Part B) must go
through this, both at registration-time verification and any future
avry-backend-side runtime call. Cerveau's own Rust runtime calls get an
independent, mirrored implementation (crates/zeroclaw-runtime) since the
Postgres connection and decryption both stay in exactly one process each,
per ADR-006 §B3/§B4 — this module is not imported by or shared with Cerveau.

The whole Aivory stack is colocated on one VPS via loopback ports. A
tenant-registered URL of e.g. `https://internal-lookalike.example/` that
resolves to `127.0.0.1` is the single most dangerous payload this
architecture can receive — it would let "MCP server verification" probe
Cerveau's own webhook or any other loopback-bound internal service from
inside Aivory's own trust boundary. Controls (ADR-006 §B4):

  1. https:// only, rejected at parse time before any network call.
  2. DNS resolved explicitly; every resolved address validated against a
     deny-list (loopback / RFC1918 / link-local incl. cloud metadata IPs /
     reserved / multicast / unspecified) before any connection is attempted.
     If ANY resolved answer is denied, the whole hostname is rejected — not
     just that one address — since a client can reconnect on any answer a
     multi-A-record host returns.
  3. The validated IP is pinned for the actual socket connection; Host/SNI
     stay the original hostname. Resolve-once-connect-to-that-exact-address,
     not resolve-then-let-the-client-reresolve — closes the DNS-rebinding
     TOCTOU gap validate-then-reconnect would leave open.
  4. No automatic redirect-following — every redirect target is re-validated
     from scratch (steps 1-3) before being followed, up to a small hop cap.
  5. Every call re-validates from scratch — "verified once" at registration
     time is never a permanent bypass for later calls.
  6. Response capped (~256 KB) via a streaming byte-counter, not
     Content-Length (a tenant server can lie about or omit it).
  7. Bounded connect (~3s) and total wall-clock timeouts.
"""

import http.client
import ipaddress
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_TOTAL_TIMEOUT = 10.0
MAX_REDIRECTS = 3
_READ_CHUNK = 8192
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# stdlib ipaddress.is_private does NOT cover this range (verified against
# Python 3.13) — RFC6598 "Shared Address Space" for carrier-grade NAT,
# routable only within an ISP's own network. Explicit check, not assumed
# covered by is_private.
_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")


class GuardedFetchError(Exception):
    """Raised for any SSRF-guard rejection or network failure. The message
    is safe to surface to the tenant (no internal detail leaked)."""


@dataclass
class GuardedResponse:
    status: int
    headers: Dict[str, str]
    body: bytes
    final_url: str


def _is_ip_denied(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable -> deny; never allow through on doubt

    # Unwrap IPv4-mapped IPv6 (::ffff:a.b.c.d) so a mapped private/loopback
    # address can't slip past the plain-IPv6-shaped checks below.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_RANGE:
        return True

    return (
        ip.is_loopback
        or ip.is_private  # covers RFC1918 (10/8, 172.16/12, 192.168/16)
        or ip.is_link_local  # covers 169.254.0.0/16 — AWS/GCP/Azure's
        # 169.254.169.254 *and* Tencent Cloud's own 169.254.0.23 metadata
        # endpoint (this VPS is Tencent) — and fe80::/10
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_pin(hostname: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise GuardedFetchError(f"DNS resolution failed for '{hostname}'") from e
    if not infos:
        raise GuardedFetchError(f"DNS resolution returned no addresses for '{hostname}'")

    resolved_ips = {info[4][0] for info in infos}
    if any(_is_ip_denied(ip) for ip in resolved_ips):
        raise GuardedFetchError(f"'{hostname}' resolves to a disallowed address")

    # Prefer IPv4 for determinism; fall back to whatever resolved.
    ipv4 = [info[4][0] for info in infos if info[0] == socket.AF_INET]
    return ipv4[0] if ipv4 else infos[0][4][0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Identical to http.client.HTTPSConnection.connect() except the raw TCP
    connect target is the pre-validated pinned IP, not a fresh resolve of
    self.host — SNI and certificate hostname verification still use
    self.host (the original hostname), so TLS validation is unweakened."""

    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: float, context: ssl.SSLContext):
        super().__init__(hostname, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def guarded_fetch(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> GuardedResponse:
    """SSRF-safe HTTPS fetch. Raises GuardedFetchError on any guard
    rejection, timeout, or transport failure — never returns a response
    for a disallowed target."""
    deadline = time.monotonic() + total_timeout
    current_url = url
    req_headers = dict(headers or {})

    for _hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current_url)
        if parts.scheme != "https":
            raise GuardedFetchError("only https:// URLs are allowed")
        if not parts.hostname:
            raise GuardedFetchError("URL has no hostname")

        port = parts.port or 443
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GuardedFetchError("request timed out")

        pinned_ip = _resolve_and_pin(parts.hostname, port)

        path = parts.path or "/"
        if parts.query:
            path += f"?{parts.query}"

        conn = _PinnedHTTPSConnection(
            parts.hostname,
            pinned_ip,
            port,
            timeout=min(connect_timeout, remaining),
            context=ssl.create_default_context(),
        )
        try:
            conn.connect()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GuardedFetchError("request timed out")
            conn.sock.settimeout(remaining)

            conn.putrequest(method, path)
            for k, v in req_headers.items():
                conn.putheader(k, v)
            if body:
                conn.putheader("Content-Length", str(len(body)))
            conn.putheader("Connection", "close")
            conn.endheaders(body if body else None)

            resp = conn.getresponse()
            resp_headers = {k: v for k, v in resp.getheaders()}

            if resp.status in _REDIRECT_STATUSES:
                location = resp_headers.get("Location") or resp_headers.get("location")
                if not location:
                    raise GuardedFetchError(f"redirect ({resp.status}) with no Location header")
                current_url = urljoin(current_url, location)
                continue  # re-validated from scratch at the top of the loop

            chunks = []
            total = 0
            while True:
                if time.monotonic() > deadline:
                    raise GuardedFetchError("request timed out while reading response")
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise GuardedFetchError(f"response exceeded {max_bytes}-byte cap")
                chunks.append(chunk)

            return GuardedResponse(
                status=resp.status,
                headers=resp_headers,
                body=b"".join(chunks),
                final_url=current_url,
            )
        except GuardedFetchError:
            raise
        except (socket.timeout, TimeoutError):
            raise GuardedFetchError("connection timed out")
        except ssl.SSLError as e:
            raise GuardedFetchError(f"TLS error: {e}")
        except OSError as e:
            raise GuardedFetchError(f"connection failed: {e}")
        finally:
            conn.close()

    raise GuardedFetchError(f"too many redirects (> {MAX_REDIRECTS})")
