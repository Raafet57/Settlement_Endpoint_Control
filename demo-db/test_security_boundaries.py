#!/usr/bin/env python3
"""SEC-S10 regression suite for localhost input-boundary vulnerabilities.

These tests encode the approved input-boundary policy for the DB-backed demo
API (``demo-db/app.py``) and its UI (``demo-db/index.html``). Each rejection
class asserts an exact deterministic contract: a specific HTTP status and a
stable JSON ``error`` code, plus a no-write assertion on the persistence store.

The three findings under regression (all independently VERIFIED in
``docs/product-shaping/fable-ultracode-product-review-2026-07-10.md``):

* F-G  stored-injection: unsanitized ``action_type`` persisted then rendered via
       unescaped ``innerHTML`` (``app.py:140,151-153``; ``index.html:135,6``).
* F-H  CSRF-reachable write: no Origin/CSRF check; body parsed as JSON
       regardless of ``Content-Type`` (``app.py:246-267,256``).
* F-Q  robustness: non-dict JSON body and non-numeric ``Content-Length`` crash
       uncaught (``app.py:255,264-267,139``).

Security regression matrix (attack surface -> exact deterministic outcome):

    | # | class          | attack vector                              | status | error code               |
    |---|----------------|--------------------------------------------|--------|--------------------------|
    | 1 | content-type   | unsupported or missing Content-Type        |   415  | unsupported_media_type   |
    | 2 | origin         | untrusted/null/cross-port/rebind origin    |   403  | origin_not_allowed       |
    | 3 | framing        | non-numeric or negative Content-Length     |   400  | invalid_content_length   |
    | 4 | framing        | missing Content-Length                     |   411  | length_required          |
    | 5 | framing        | declared length larger than received body  |   400  | incomplete_body          |
    | 6 | size           | declared or actual oversized request       |   413  | request_too_large        |
    | 7 | body-shape     | malformed UTF-8 body                        |   400  | invalid_utf8             |
    | 8 | body-shape     | malformed JSON or empty body               |   400  | invalid_json             |
    | 9 | body-shape     | non-object JSON                             |   400  | invalid_payload          |
    |10 | values         | invalid or missing action_type             |   422  | invalid_action_type      |
    |11 | values         | invalid or missing actor                   |   422  | invalid_actor            |
    |12 | injection      | HTML-like action_type (F-G server half)    |   422  | invalid_action_type + never persisted |
    |13 | ui-sink        | index.html renders API data via innerHTML  |  n/a   | no innerHTML sink        |

Positive anchors (must be GREEN now AND after the GREEN fix):

    * valid localhost JSON action persists exactly one operator action + one audit event
    * application/json with a charset parameter is accepted
    * Origin absent (non-browser JSON client) is accepted
    * malformed JSON already returns a deterministic 400 invalid_json without writing

SEC-S10 fail-closed hardening (added by this change):

    | # | class          | attack vector                              | status | error code             |
    |---|----------------|--------------------------------------------|--------|------------------------|
    |14 | origin         | malformed bracket / nonnumeric-port origin |   403  | origin_not_allowed     |
    |15 | framing        | duplicate/conflicting Content-Length       |   400  | invalid_content_length |
    |16 | framing        | Transfer-Encoding on fixed-length endpoint |   400  | invalid_content_length |
    |17 | framing        | extremely long all-digit Content-Length    |   413  | request_too_large      |
    |18 | body-shape     | non-RFC JSON NaN/Infinity/-Infinity        |   400  | invalid_json           |

All fixtures are synthetic, localhost-only, and make no external network calls.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "seed.py"
APP = ROOT / "app.py"
INDEX = ROOT / "index.html"

# Approved policy allowlists (exact, no coercion, no missing-field defaults).
ALLOWED_ACTION_TYPES = ("open_repair_task", "approve_fallback", "hold_payment")
ALLOWED_ACTORS = ("demo_operator", "ops_analyst")

# A body clearly above any plausible "small named" two-short-string limit, yet
# small enough to sit inside the loopback socket buffer (so the client's send
# never blocks when the server rejects by declared length before reading).
OVERSIZED_BYTES = 4096
RAW_TIMEOUT = 4.0
TARGET_SLUG = "blocked"

Response = namedtuple("Response", ["status", "headers", "text", "json"])


def _parse_response(raw: bytes) -> Response:
    """Parse a raw HTTP/1.x response; status is None if the peer sent nothing."""
    if not raw:
        return Response(None, {}, "", None)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = None
    if lines:
        parts = lines[0].split(b" ")
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, _, value = line.partition(b":")
            headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    text = body.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return Response(status, headers, text, parsed)


class LocalhostActionBoundaryTests(unittest.TestCase):
    """Real localhost HTTP behavior against a freshly reseeded synthetic DB."""

    @classmethod
    def setUpClass(cls) -> None:
        # Reseed only the ignored synthetic demo database (demo-db/data/ is gitignored).
        subprocess.run(
            [sys.executable, str(SEED)],
            check=True,
            text=True,
            capture_output=True,
        )
        cls.host = "127.0.0.1"
        cls.port = cls._free_port()
        cls.base = f"http://{cls.host}:{cls.port}"
        cls.origin = f"http://{cls.host}:{cls.port}"
        cls.proc = subprocess.Popen(
            [sys.executable, str(APP), "--host", cls.host, "--port", str(cls.port)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cls._wait_ready()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=RAW_TIMEOUT)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=RAW_TIMEOUT)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @classmethod
    def _wait_ready(cls) -> None:
        last: Exception | None = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"{cls.base}/readyz", timeout=RAW_TIMEOUT) as resp:
                    if json.loads(resp.read().decode("utf-8")).get("status") == "ok":
                        return
            except Exception as exc:  # noqa: BLE001 - surface final error after retries
                last = exc
                time.sleep(0.1)
        raise RuntimeError(f"demo API did not become ready: {last}")

    # -- helpers -----------------------------------------------------------

    def get_json(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=RAW_TIMEOUT) as resp:  # noqa: S310 - localhost test only
            return json.loads(resp.read().decode("utf-8"))

    def counts(self, slug: str = TARGET_SLUG) -> tuple[int, int]:
        payload = self.get_json(f"/api/scenarios/{slug}")
        return (len(payload["operator_actions"]), len(payload["audit_events"]))

    def raw_http(self, head_lines: list[str], body: bytes = b"") -> Response:
        """Send a fully caller-controlled request; bounded so it can never hang."""
        head = ("\r\n".join(head_lines) + "\r\n\r\n").encode("latin-1")
        with socket.create_connection((self.host, self.port), timeout=RAW_TIMEOUT) as sock:
            sock.settimeout(RAW_TIMEOUT)
            sock.sendall(head + body)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except socket.timeout:
                pass  # bounded: proves the endpoint does not hang on malformed framing
        return _parse_response(b"".join(chunks))

    def action_request(
        self,
        body: bytes,
        *,
        slug: str = TARGET_SLUG,
        content_type: str | None = "application/json",
        origin: str | None = "__server__",
        host: str | None = None,
        content_length: str | None = "__auto__",
    ) -> Response:
        host = host or f"{self.host}:{self.port}"
        lines = [f"POST /api/scenarios/{slug}/actions HTTP/1.1", f"Host: {host}"]
        if content_type is not None:
            lines.append(f"Content-Type: {content_type}")
        if origin == "__server__":
            origin = self.origin
        if origin is not None:
            lines.append(f"Origin: {origin}")
        if content_length == "__auto__":
            content_length = str(len(body))
        if content_length is not None:
            lines.append(f"Content-Length: {content_length}")
        lines.append("Connection: close")
        return self.raw_http(lines, body)

    def assert_rejected(self, resp: Response, status: int, error: str, ctx: str) -> None:
        self.assertEqual(
            resp.status,
            status,
            f"{ctx}: expected HTTP {status}, got status={resp.status} body={resp.text!r}",
        )
        self.assertIsInstance(
            resp.json,
            dict,
            f"{ctx}: expected a JSON error body, got {resp.text!r}",
        )
        self.assertEqual(
            resp.json.get("error"),
            error,
            f"{ctx}: expected stable JSON error code {error!r}, got {resp.json!r}",
        )

    def assert_no_write(self, before: tuple[int, int], ctx: str, slug: str = TARGET_SLUG) -> None:
        after = self.counts(slug)
        self.assertEqual(
            after,
            before,
            f"{ctx}: rejected request must not write; operator/audit counts changed {before} -> {after}",
        )

    def valid_body(self) -> bytes:
        return json.dumps({"action_type": "open_repair_task", "actor": "demo_operator"}).encode("utf-8")

    # == F-H: Content-Type enforcement -> 415 unsupported_media_type ========

    def test_content_type_must_be_application_json(self) -> None:
        for label, content_type in [
            ("text_plain_csrf_bypass", "text/plain"),
            ("form_urlencoded", "application/x-www-form-urlencoded"),
            ("missing_content_type", None),
        ]:
            with self.subTest(vector=label):
                before = self.counts()
                resp = self.action_request(self.valid_body(), content_type=content_type)
                self.assert_rejected(resp, 415, "unsupported_media_type", label)
                self.assert_no_write(before, label)

    # == F-H: Origin / CSRF enforcement -> 403 origin_not_allowed ===========

    def test_untrusted_origin_is_rejected(self) -> None:
        for label, origin in [
            ("arbitrary_cross_origin", "http://evil.example"),
            ("null_opaque_origin", "null"),
            ("cross_port_loopback", f"http://{self.host}:{self.port + 1}"),
        ]:
            with self.subTest(vector=label):
                before = self.counts()
                resp = self.action_request(self.valid_body(), origin=origin)
                self.assert_rejected(resp, 403, "origin_not_allowed", label)
                self.assert_no_write(before, label)

    def test_dns_rebinding_non_loopback_host_origin_pair_rejected(self) -> None:
        before = self.counts()
        resp = self.action_request(
            self.valid_body(),
            origin="http://rebind.attacker.example",
            host="rebind.attacker.example",
        )
        self.assert_rejected(resp, 403, "origin_not_allowed", "dns_rebinding_pair")
        self.assert_no_write(before, "dns_rebinding_pair")

    def test_origin_must_be_a_bare_origin(self) -> None:
        # A well-formed Origin is scheme://host[:port] only. Even when the
        # authority matches Host, extra URL parts (userinfo/path/query/fragment)
        # mean the value is not a bare origin and must be rejected.
        authority = f"{self.host}:{self.port}"
        for label, origin in [
            ("origin_with_path", f"http://{authority}/path"),
            ("origin_with_query", f"http://{authority}?probe=1"),
            ("origin_with_fragment", f"http://{authority}#frag"),
            ("origin_with_userinfo", f"http://user:pass@{authority}"),
        ]:
            with self.subTest(vector=label):
                before = self.counts()
                resp = self.action_request(self.valid_body(), origin=origin)
                self.assert_rejected(resp, 403, "origin_not_allowed", label)
                self.assert_no_write(before, label)

    def test_origin_validation_is_total_over_malformed_authority(self) -> None:
        # Origin validation must be total for attacker-controlled text. A
        # malformed authority (unmatched IPv6 bracket) makes urllib.parse raise,
        # and a nonnumeric port is not a valid bare HTTP origin even when the
        # Origin authority exactly equals Host. Both must be a deterministic
        # 403 origin_not_allowed with no write -- never an uncaught crash and
        # never an accepted request.
        for label, origin, host in [
            ("malformed_ipv6_bracket", "http://[::1", None),
            ("nonnumeric_port_matching_host", f"http://{self.host}:notaport", f"{self.host}:notaport"),
        ]:
            with self.subTest(vector=label):
                before = self.counts()
                resp = self.action_request(self.valid_body(), origin=origin, host=host)
                self.assert_rejected(resp, 403, "origin_not_allowed", label)
                self.assert_no_write(before, label)

    def test_require_allowed_origin_is_strict_over_raw_headers(self) -> None:
        # Origin validation must be strict over the RAW attacker-controlled
        # header text. A supplied Origin is trusted only when it is exactly one
        # bare ASCII http://127.0.0.1[:port] or http://localhost[:port] whose
        # authority equals the single Host authority. Empty query/fragment
        # delimiters and whitespace/tabs -- trailing or embedded, each stripped
        # away by urlparse -- plus a duplicate Origin or Host whose first value
        # matches must all fail closed with 403 origin_not_allowed and no write.
        body = self.valid_body()
        authority = f"{self.host}:{self.port}"
        good_origin = f"http://{authority}"

        def raw_action(header_lines: list[str]) -> Response:
            lines = [
                f"POST /api/scenarios/{TARGET_SLUG}/actions HTTP/1.1",
                *header_lines,
                "Content-Type: application/json",
                f"Content-Length: {len(body)}",
                "Connection: close",
            ]
            return self.raw_http(lines, body)

        cases = [
            ("empty_query_delimiter", [f"Host: {authority}", f"Origin: {good_origin}?"]),
            ("empty_fragment_delimiter", [f"Host: {authority}", f"Origin: {good_origin}#"]),
            ("trailing_whitespace_tab", [f"Host: {authority}", f"Origin: {good_origin}\t"]),
            ("embedded_tab", [f"Host: {authority}", f"Origin: http://{self.host}\t:{self.port}"]),
            (
                "duplicate_origin_first_matching",
                [f"Host: {authority}", f"Origin: {good_origin}", "Origin: http://evil.example"],
            ),
            (
                "duplicate_host_first_matching",
                [f"Host: {authority}", "Host: evil.example", f"Origin: {good_origin}"],
            ),
        ]
        for label, header_lines in cases:
            with self.subTest(vector=label):
                before = self.counts()
                resp = raw_action(header_lines)
                self.assert_rejected(resp, 403, "origin_not_allowed", label)
                self.assert_no_write(before, label)

    # == F-Q: Content-Length framing ========================================

    def test_non_numeric_content_length_rejected_without_hanging(self) -> None:
        before = self.counts()
        resp = self.action_request(self.valid_body(), content_length="not-a-number")
        self.assert_rejected(resp, 400, "invalid_content_length", "content_length_non_numeric")
        self.assert_no_write(before, "content_length_non_numeric")

    def test_negative_content_length_rejected_without_hanging(self) -> None:
        before = self.counts()
        resp = self.action_request(self.valid_body(), content_length="-1")
        self.assert_rejected(resp, 400, "invalid_content_length", "content_length_negative")
        self.assert_no_write(before, "content_length_negative")

    def test_missing_content_length_rejected(self) -> None:
        before = self.counts()
        resp = self.action_request(self.valid_body(), content_length=None)
        self.assert_rejected(resp, 411, "length_required", "content_length_missing")
        self.assert_no_write(before, "content_length_missing")

    def test_content_length_must_be_ascii_digits_only(self) -> None:
        # HTTP Content-Length grammar is 1*DIGIT (ASCII 0-9). int() is more
        # permissive (accepts a leading '+' and Python underscore separators);
        # that permissiveness must not be treated as valid HTTP grammar. Each
        # value below parses via int() to exactly the real body length, so the
        # only thing under test is the grammar rejection, not a size mismatch.
        body = self.valid_body()
        digits = str(len(body))
        for label, content_length in [
            ("leading_plus", "+" + digits),
            ("underscore_separator", digits[:1] + "_" + digits[1:]),
        ]:
            with self.subTest(vector=label):
                before = self.counts()
                resp = self.action_request(body, content_length=content_length)
                self.assert_rejected(resp, 400, "invalid_content_length", label)
                self.assert_no_write(before, label)

    def test_content_length_exceeding_body_rejected_without_hanging(self) -> None:
        # Declared length stays within the small named limit but exceeds the
        # bytes actually delivered before the peer closes -> short/incomplete body.
        body = self.valid_body()
        before = self.counts()
        resp = self.action_request(body, content_length=str(len(body) + 16))
        self.assert_rejected(resp, 400, "incomplete_body", "content_length_exceeds_body")
        self.assert_no_write(before, "content_length_exceeds_body")

    def test_oversized_payload_rejected_without_hanging(self) -> None:
        payload = {"action_type": "open_repair_task", "actor": "demo_operator", "note": "A" * OVERSIZED_BYTES}
        body = json.dumps(payload).encode("utf-8")
        before = self.counts()
        resp = self.action_request(body)
        self.assert_rejected(resp, 413, "request_too_large", "oversized_payload")
        self.assert_no_write(before, "oversized_payload")

    def framing_request(self, body: bytes, framing_headers: list[str]) -> Response:
        """POST a valid-authority action with caller-chosen framing headers."""
        lines = [
            f"POST /api/scenarios/{TARGET_SLUG}/actions HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Content-Type: application/json",
            f"Origin: {self.origin}",
        ]
        lines.extend(framing_headers)
        lines.append("Connection: close")
        return self.raw_http(lines, body)

    def test_ambiguous_http_framing_is_failed_closed(self) -> None:
        # A fixed-length JSON endpoint accepts exactly one Content-Length and no
        # Transfer-Encoding. Duplicate/conflicting Content-Length (inspecting all
        # instances, not just the first) and any Transfer-Encoding are request-
        # smuggling vectors and must be rejected deterministically before any
        # body read or persistence, using the invalid_content_length contract.
        body = self.valid_body()
        real = str(len(body))
        for label, framing in [
            ("duplicate_identical_content_length", [f"Content-Length: {real}", f"Content-Length: {real}"]),
            ("duplicate_conflicting_content_length", [f"Content-Length: {real}", f"Content-Length: {len(body) + 64}"]),
            ("transfer_encoding_plus_content_length", [f"Content-Length: {real}", "Transfer-Encoding: chunked"]),
        ]:
            with self.subTest(vector=label):
                before = self.counts()
                resp = self.framing_request(body, framing)
                self.assert_rejected(resp, 400, "invalid_content_length", label)
                self.assert_no_write(before, label)

    def test_extremely_long_digit_content_length_rejected_without_hanging(self) -> None:
        # An extremely long all-digit Content-Length is valid DIGIT grammar, so
        # it must be compared to the size bound WITHOUT an unbounded int()
        # conversion (int() raises past CPython's integer-string digit limit on
        # 3.11+ and 3.9.14+/3.10.7+ backports). A declared length above the small
        # named limit stays a deterministic 413 request_too_large with no write.
        body = self.valid_body()
        before = self.counts()
        resp = self.action_request(body, content_length="9" * 5000)
        self.assert_rejected(resp, 413, "request_too_large", "extremely_long_digit_content_length")
        self.assert_no_write(before, "extremely_long_digit_content_length")

    # == F-Q: body shape ====================================================

    def test_non_dict_json_body_rejected(self) -> None:
        for label, body in [
            ("json_array", b"[]"),
            ("json_string", b'"just-a-string"'),
            ("json_number", b"123"),
            ("json_null", b"null"),
        ]:
            with self.subTest(vector=label):
                before = self.counts()
                resp = self.action_request(body)
                self.assert_rejected(resp, 400, "invalid_payload", label)
                self.assert_no_write(before, label)

    def test_malformed_utf8_body_rejected(self) -> None:
        before = self.counts()
        resp = self.action_request(b"\xff\xfe\xfa")
        self.assert_rejected(resp, 400, "invalid_utf8", "malformed_utf8")
        self.assert_no_write(before, "malformed_utf8")

    def test_empty_body_rejected(self) -> None:
        before = self.counts()
        resp = self.action_request(b"")
        self.assert_rejected(resp, 400, "invalid_json", "empty_body")
        self.assert_no_write(before, "empty_body")

    def test_non_rfc_json_constants_rejected(self) -> None:
        # RFC 8259 JSON has no NaN/Infinity/-Infinity; Python's json.loads admits
        # them by default. Each must be a deterministic 400 invalid_json with no
        # write, even when action_type and actor are otherwise allowlisted.
        for label, token in [("nan", "NaN"), ("infinity", "Infinity"), ("negative_infinity", "-Infinity")]:
            with self.subTest(vector=label):
                before = self.counts()
                body = (
                    '{"action_type": "open_repair_task", "actor": "demo_operator", "note": %s}' % token
                ).encode("utf-8")
                resp = self.action_request(body)
                self.assert_rejected(resp, 400, "invalid_json", label)
                self.assert_no_write(before, label)

    # == F-G + values: allowlist, no coercion, no missing-field defaults ====

    def test_invalid_field_values_rejected(self) -> None:
        for label, payload, status, error in [
            ("action_type_not_in_allowlist", {"action_type": "delete_everything", "actor": "demo_operator"}, 422, "invalid_action_type"),
            ("action_type_type_coercion_int", {"action_type": 123, "actor": "demo_operator"}, 422, "invalid_action_type"),
            ("missing_action_type_no_default", {"actor": "demo_operator"}, 422, "invalid_action_type"),
            ("actor_not_in_allowlist", {"action_type": "open_repair_task", "actor": "intruder"}, 422, "invalid_actor"),
            ("actor_type_coercion_list", {"action_type": "open_repair_task", "actor": ["ops_analyst"]}, 422, "invalid_actor"),
            ("missing_actor_no_default", {"action_type": "open_repair_task"}, 422, "invalid_actor"),
        ]:
            with self.subTest(vector=label):
                before = self.counts()
                resp = self.action_request(json.dumps(payload).encode("utf-8"))
                self.assert_rejected(resp, status, error, label)
                self.assert_no_write(before, label)

    def test_html_like_action_type_rejected_and_never_persisted(self) -> None:
        malicious = "<img src=x onerror=alert(document.domain)>"
        body = json.dumps({"action_type": malicious, "actor": "demo_operator"}).encode("utf-8")
        before = self.counts()
        resp = self.action_request(body)
        self.assert_rejected(resp, 422, "invalid_action_type", "html_like_action_type")
        self.assert_no_write(before, "html_like_action_type")
        persisted = self.get_json(f"/api/scenarios/{TARGET_SLUG}")["operator_actions"]
        self.assertFalse(
            any(action.get("action_type") == malicious for action in persisted),
            "HTML-like action_type must never be persisted (F-G stored-injection source)",
        )

    # == Positive anchors (expected GREEN now and after the fix) ============

    def test_valid_json_action_persists_exactly_one(self) -> None:
        before = self.counts()
        resp = self.action_request(self.valid_body())
        self.assertEqual(resp.status, 201, f"valid action must be accepted, got {resp.status} {resp.text!r}")
        self.assertEqual(resp.json.get("status"), "action_recorded")
        after = self.counts()
        self.assertEqual(
            after,
            (before[0] + 1, before[1] + 1),
            f"valid action must persist exactly one operator action and one audit event {before} -> {after}",
        )

    def test_application_json_charset_parameter_accepted(self) -> None:
        before = self.counts()
        body = json.dumps({"action_type": "approve_fallback", "actor": "ops_analyst"}).encode("utf-8")
        resp = self.action_request(body, content_type="application/json; charset=utf-8")
        self.assertEqual(resp.status, 201, f"charset parameter must remain valid, got {resp.status} {resp.text!r}")
        after = self.counts()
        self.assertEqual(after, (before[0] + 1, before[1] + 1))

    def test_valid_action_without_origin_accepted(self) -> None:
        before = self.counts()
        body = json.dumps({"action_type": "hold_payment", "actor": "demo_operator"}).encode("utf-8")
        resp = self.action_request(body, origin=None)
        self.assertEqual(resp.status, 201, f"non-browser JSON client (no Origin) must be accepted, got {resp.status}")
        after = self.counts()
        self.assertEqual(after, (before[0] + 1, before[1] + 1))

    def test_malformed_json_returns_deterministic_400_without_writing(self) -> None:
        before = self.counts()
        resp = self.action_request(b"{not valid json")
        self.assertEqual(resp.status, 400, f"malformed JSON must be a deterministic 400, got {resp.status}")
        self.assertIsInstance(resp.json, dict)
        self.assertEqual(resp.json.get("error"), "invalid_json")
        self.assert_no_write(before, "malformed_json")


class IndexHtmlInnerHtmlSinkTests(unittest.TestCase):
    """F-G UI half: API-derived values must reach the DOM without an innerHTML sink."""

    ASSIGN_RE = re.compile(r"\.innerHTML\s*=(?!=)")
    INTERPOLATED_SINK_RE = re.compile(r"\.innerHTML\s*=[^;]*\$\{")

    def setUp(self) -> None:
        self.html = INDEX.read_text(encoding="utf-8")

    def test_no_innerhtml_assignment_sink(self) -> None:
        sinks = self.ASSIGN_RE.findall(self.html)
        self.assertEqual(
            sinks,
            [],
            f"demo-db/index.html must not assign innerHTML (found {len(sinks)} sinks); "
            "API-derived values must use DOM construction / textContent",
        )

    def test_no_dynamic_data_interpolated_into_innerhtml(self) -> None:
        interpolated = self.INTERPOLATED_SINK_RE.findall(self.html)
        self.assertEqual(
            interpolated,
            [],
            f"demo-db/index.html interpolates dynamic values into innerHTML "
            f"({len(interpolated)} sites); this is the F-G stored-injection sink",
        )

    def test_operator_actions_not_rendered_via_innerhtml(self) -> None:
        self.assertNotIn(
            "operatorActions').innerHTML",
            self.html,
            "persisted action_type must not be rendered through an innerHTML sink (F-G)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
