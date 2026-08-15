#!/usr/bin/env python3
"""
Adversarial test matrix for app/services/guarded_fetch.py (ADR-006 §B4 / B1
exit gate). Pure-stdlib — guarded_fetch.py has zero third-party deps, so
this runs with plain `python3 -m unittest` or `python3 tests/test_guarded_fetch.py`,
no venv/requirements install needed, and hits no real network.

Exit gate (ADR-006 phasing table, row B1): 100% of this matrix green before
anything tenant-facing ships — loopback, RFC1918, both cloud metadata IPs
(169.254.169.254 + Tencent's 169.254.0.23), a DNS-rebinding shape, an
oversized response, and a redirect-to-private-IP, all denied.
"""

import ipaddress
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.guarded_fetch import (  # noqa: E402
    MAX_REDIRECTS,
    GuardedFetchError,
    _is_ip_denied,
    _resolve_and_pin,
    guarded_fetch,
)


class DenyListMatrix(unittest.TestCase):
    DENIED = {
        "loopback v4": "127.0.0.1",
        "loopback v4 alt": "127.5.5.5",
        "loopback v6": "::1",
        "rfc1918 10/8": "10.0.0.5",
        "rfc1918 172.16/12": "172.16.0.1",
        "rfc1918 172.31/12 boundary": "172.31.255.255",
        "rfc1918 192.168/16": "192.168.1.1",
        "link-local AWS/GCP/Azure metadata": "169.254.169.254",
        "link-local Tencent metadata": "169.254.0.23",
        "link-local v6": "fe80::1",
        "unspecified v4": "0.0.0.0",
        "unspecified v6": "::",
        "multicast v4": "224.0.0.1",
        "multicast v6": "ff02::1",
        "reserved (240/4)": "240.0.0.1",
        "ipv4-mapped loopback": "::ffff:127.0.0.1",
        "ipv4-mapped rfc1918": "::ffff:10.1.2.3",
        "ipv4-mapped metadata": "::ffff:169.254.169.254",
        "carrier-grade NAT (RFC6598)": "100.64.0.1",
        "unparseable garbage": "not-an-ip",
    }

    ALLOWED = {
        "public v4 (google dns)": "8.8.8.8",
        "public v4 (cloudflare)": "1.1.1.1",
        "public v6 (google dns)": "2001:4860:4860::8888",
    }

    def test_denied_addresses(self):
        for label, ip in self.DENIED.items():
            with self.subTest(label=label, ip=ip):
                self.assertTrue(_is_ip_denied(ip), f"{label} ({ip}) should be denied")

    def test_allowed_addresses(self):
        for label, ip in self.ALLOWED.items():
            with self.subTest(label=label, ip=ip):
                self.assertFalse(_is_ip_denied(ip), f"{label} ({ip}) should be allowed")

    def test_carrier_grade_nat_not_covered_by_stdlib_is_private(self):
        # Documents WHY guarded_fetch.py has an explicit _CGNAT_RANGE check:
        # stdlib ipaddress.is_private does NOT cover RFC6598 100.64.0.0/10
        # (verified against Python 3.13) — if a future Python version starts
        # covering it, that's fine, but _is_ip_denied must not silently
        # start relying on stdlib behavior this test didn't re-verify.
        self.assertFalse(ipaddress.ip_address("100.64.0.1").is_private)
        self.assertTrue(_is_ip_denied("100.64.0.1"))


class ResolveAndPin(unittest.TestCase):
    def _fake_getaddrinfo(self, ips, port=443, family=socket.AF_INET):
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    def test_rejects_when_any_answer_is_private(self):
        """The DNS-rebinding shape: a hostname that resolves to BOTH a
        public and a private address must be rejected outright, not just
        skip the private answer — a client can reconnect on any answer."""
        with patch("socket.getaddrinfo", return_value=self._fake_getaddrinfo(["8.8.8.8", "127.0.0.1"])):
            with self.assertRaises(GuardedFetchError):
                _resolve_and_pin("rebinding.example", 443)

    def test_rejects_pure_private_hostname(self):
        with patch("socket.getaddrinfo", return_value=self._fake_getaddrinfo(["169.254.169.254"])):
            with self.assertRaises(GuardedFetchError):
                _resolve_and_pin("metadata.example", 443)

    def test_accepts_pure_public_hostname_and_pins_first_v4(self):
        with patch("socket.getaddrinfo", return_value=self._fake_getaddrinfo(["8.8.8.8", "8.8.4.4"])):
            pinned = _resolve_and_pin("dns.example", 443)
        self.assertEqual(pinned, "8.8.8.8")

    def test_dns_failure_raises_guarded_error_not_socket_error(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("nxdomain")):
            with self.assertRaises(GuardedFetchError):
                _resolve_and_pin("doesnotexist.example", 443)


class SchemeAndParseEnforcement(unittest.TestCase):
    def test_http_scheme_rejected_before_any_dns_call(self):
        with patch("socket.getaddrinfo") as mock_resolve:
            with self.assertRaises(GuardedFetchError):
                guarded_fetch("http://example.com/mcp")
            mock_resolve.assert_not_called()

    def test_missing_hostname_rejected(self):
        with self.assertRaises(GuardedFetchError):
            guarded_fetch("https:///no-host")


def _fake_response(status=200, headers=None, chunks=(b"",)):
    resp = MagicMock()
    resp.status = status
    resp.getheaders.return_value = list((headers or {}).items())
    iterator = iter(chunks)

    def _read(_n=None):
        try:
            return next(iterator)
        except StopIteration:
            return b""

    resp.read.side_effect = _read
    return resp


class EndToEndMocked(unittest.TestCase):
    """Exercises guarded_fetch()'s full loop (redirect handling, size cap,
    connection pinning) with the actual socket/TLS layer mocked out — no
    real network I/O, fully deterministic, CI-safe."""

    def _patch_connection(self, response_sequence):
        """response_sequence: list of fake response objects, one per hop."""
        conn_instances = []

        def _make_conn(hostname, pinned_ip, port, timeout, context):
            conn = MagicMock()
            conn.host = hostname
            conn._pinned_ip = pinned_ip
            conn.sock = MagicMock()
            conn.getresponse.return_value = response_sequence[len(conn_instances)]
            conn_instances.append(conn)
            return conn

        return conn_instances, _make_conn

    def test_dns_rebinding_pin_used_for_actual_connect(self):
        """Resolve once, connect to exactly that address — even if a second
        resolve (were one to happen) would return something else. Proves
        pin-then-connect atomicity, not resolve-then-reresolve."""
        calls = []
        with patch(
            "socket.getaddrinfo",
            side_effect=[
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],  # would-be rebind
            ],
        ):
            with patch("app.services.guarded_fetch._PinnedHTTPSConnection") as MockConn:
                instance = MockConn.return_value
                instance.getresponse.return_value = _fake_response(200, {}, [b"ok", b""])
                guarded_fetch("https://rebind.example/tools")
                calls.append(MockConn.call_args)
        # Constructed with the FIRST resolution's pinned IP only — the
        # (unused) second getaddrinfo return value proves nothing rebinds
        # the already-pinned connection mid-flight.
        _, kwargs = calls[0]
        args = calls[0][0]
        pinned_ip_arg = args[1] if len(args) > 1 else kwargs.get("pinned_ip")
        self.assertEqual(pinned_ip_arg, "8.8.8.8")

    def test_size_cap_aborts_before_reading_entire_oversized_body(self):
        big_chunk = b"x" * (300 * 1024)  # exceeds the 256 KB cap in one chunk
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]):
            with patch("app.services.guarded_fetch._PinnedHTTPSConnection") as MockConn:
                instance = MockConn.return_value
                instance.getresponse.return_value = _fake_response(200, {}, [big_chunk, b"more", b""])
                with self.assertRaises(GuardedFetchError):
                    guarded_fetch("https://big.example/tools")
                # Only the first (oversized) chunk should ever have been read —
                # confirms we abort on crossing the cap rather than draining
                # a malicious server's entire response first.
                self.assertEqual(instance.getresponse.return_value.read.call_count, 1)

    def test_redirect_to_private_ip_is_rejected_not_followed(self):
        """A 302 pointing at a private/loopback target must be re-validated
        from scratch like any other URL, not blindly followed."""
        with patch("socket.getaddrinfo") as mock_resolve:
            mock_resolve.side_effect = [
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],  # first hop: public, fine
            ]
            with patch("app.services.guarded_fetch._PinnedHTTPSConnection") as MockConn:
                instance = MockConn.return_value
                instance.getresponse.return_value = _fake_response(
                    302, {"Location": "https://internal.example/webhook"}, [b""]
                )
                # Second hop's hostname resolves to a private IP -> must be denied.
                mock_resolve.side_effect = [
                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
                ]
                with self.assertRaises(GuardedFetchError):
                    guarded_fetch("https://public.example/tools")

    def test_redirect_downgrading_to_http_is_rejected(self):
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]):
            with patch("app.services.guarded_fetch._PinnedHTTPSConnection") as MockConn:
                instance = MockConn.return_value
                instance.getresponse.return_value = _fake_response(
                    302, {"Location": "http://public.example/tools"}, [b""]
                )
                with self.assertRaises(GuardedFetchError):
                    guarded_fetch("https://public.example/tools")

    def test_redirect_loop_hits_max_redirects_cap(self):
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]):
            with patch("app.services.guarded_fetch._PinnedHTTPSConnection") as MockConn:
                instance = MockConn.return_value
                instance.getresponse.return_value = _fake_response(
                    302, {"Location": "https://public.example/tools"}, [b""]
                )
                with self.assertRaises(GuardedFetchError) as ctx:
                    guarded_fetch("https://public.example/tools")
                self.assertIn("redirect", str(ctx.exception).lower())
                self.assertLessEqual(MockConn.call_count, MAX_REDIRECTS + 2)

    def test_successful_fetch_returns_body_and_status(self):
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]):
            with patch("app.services.guarded_fetch._PinnedHTTPSConnection") as MockConn:
                instance = MockConn.return_value
                instance.getresponse.return_value = _fake_response(200, {"Content-Type": "application/json"}, [b'{"tools":[]}', b""])
                result = guarded_fetch("https://public.example/tools")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b'{"tools":[]}')
        self.assertEqual(result.headers.get("Content-Type"), "application/json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
