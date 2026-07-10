#!/usr/bin/env python3
"""SEC-O20 focused tests for structured, non-sensitive local request/error logging.

These tests pin the contract of the structured logging added to ``demo-db/app.py``:
the localhost demo must emit diagnosable, machine-parseable events for normal
requests and for handled client/server errors, while NEVER writing any request
body, response payload, query string/value, header value (authorization/cookie/
api-key), actor/action field value, SQL, exception message, or other sensitive
identifier into a log line.

Design of the surface under test (all standard library):

* Structured events are JSON Lines on **stderr** (one JSON object per line). The
  existing ``SERVING`` readiness banner stays on **stdout** and is unchanged.
* ``app.normalized_route(raw_path)`` collapses the query string and the single
  variable slug segment to a stable route label, so no caller-controlled or
  sensitive path value ever reaches a log line.
* ``app.log_request_event(method, path, status, elapsed_ms=None)`` emits an
  ``event="request"`` record whose keys are drawn ONLY from a fixed allowlist.
* ``app.log_error_event(method, path, category, exc, status=500)`` emits an
  ``event="error"`` record carrying a stable ``category`` and the exception
  **class name only** (``type(exc).__name__``) -- never ``str(exc)``/``repr``.
* Server error responses are deterministic and expose no raw exception detail.

Contract matrix (allowlisted fields only -> everything else is excluded):

    | event   | required fields                                   | forbidden content            |
    |---------|---------------------------------------------------|------------------------------|
    | request | ts, level, event, method, path, status[, elapsed] | query/body/header/exc values |
    | error   | ts, level, event, method, path, status, category, | str(exc)/repr(exc), SQL,      |
    |         | exc_type                                           | body/header/query values     |

Secret-like values used here are OBVIOUSLY synthetic and are assembled at
runtime, so no realistic credential material is committed in tracked test source.
All fixtures are synthetic, localhost-only, and make no external network calls.
"""
from __future__ import annotations

import io
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import app

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "seed.py"
APP = ROOT / "app.py"
RAW_TIMEOUT = 4.0
TARGET_SLUG = "blocked"

# Exact field allowlists. A structured record may contain ONLY these keys; any
# additional key is a potential leak channel and fails the test.
ALLOWED_REQUEST_KEYS = {"ts", "level", "event", "method", "path", "status", "elapsed_ms"}
ALLOWED_ERROR_KEYS = {"ts", "level", "event", "method", "path", "status", "category", "exc_type"}


def synthetic_sentinel(tag: str) -> str:
    """An obviously synthetic, unmistakable marker assembled at runtime.

    Never a realistic credential value; it exists only so a test can assert the
    marker is absent from captured log output for a given attack surface.
    """
    return "SYNTHETIC-DO-NOT-LOG-" + tag + "-" + "".join(str(d) for d in range(10))


def records_from(text: str) -> list:
    """Every line of ``text`` that parses as a JSON object, in order."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ======================================================================
# Unit-level: pure functions and emitters, captured in-process.
# ======================================================================


class NormalizedRouteTests(unittest.TestCase):
    """The logged path is a stable route label: no query, no concrete slug."""

    def test_fixed_routes_pass_through(self) -> None:
        for path in ("/", "/readyz", "/api/scenarios", "/api/audit/counts", "/api/source-manifest"):
            with self.subTest(path=path):
                self.assertEqual(app.normalized_route(path), path)

    def test_variable_slug_is_collapsed(self) -> None:
        self.assertEqual(app.normalized_route("/api/scenarios/blocked"), "/api/scenarios/{slug}")
        self.assertEqual(app.normalized_route("/api/evidence/blocked"), "/api/evidence/{slug}")
        self.assertEqual(app.normalized_route("/api/scenarios/blocked/actions"), "/api/scenarios/{slug}/actions")

    def test_trailing_slash_normalized(self) -> None:
        self.assertEqual(app.normalized_route("/api/scenarios/blocked/"), "/api/scenarios/{slug}")
        self.assertEqual(app.normalized_route("/readyz/"), "/readyz")

    def test_query_string_is_stripped_and_never_leaks(self) -> None:
        sentinel = synthetic_sentinel("ROUTE-QUERY")
        result = app.normalized_route(f"/api/scenarios?token={sentinel}&password={sentinel}")
        self.assertEqual(result, "/api/scenarios")
        self.assertNotIn(sentinel, result)

    def test_sensitive_slug_segment_never_leaks(self) -> None:
        sentinel = synthetic_sentinel("ROUTE-SLUG")
        result = app.normalized_route(f"/api/scenarios/{sentinel}")
        self.assertEqual(result, "/api/scenarios/{slug}")
        self.assertNotIn(sentinel, result)

    def test_unknown_path_is_bucketed_without_caller_text(self) -> None:
        sentinel = synthetic_sentinel("ROUTE-UNKNOWN")
        result = app.normalized_route(f"/admin/{sentinel}")
        self.assertNotIn(sentinel, result)
        self.assertEqual(result, "/{other}")

    def test_total_over_empty_or_malformed_target(self) -> None:
        # A malformed request line can leave the target empty; must not raise.
        for raw in ("", "not-a-path", "//"):
            with self.subTest(raw=raw):
                self.assertIsInstance(app.normalized_route(raw), str)

    def test_malformed_bracketed_target_is_total(self) -> None:
        # An unmatched IPv6 bracket makes urllib.parse raise ValueError; the
        # label must stay total and must not echo the caller-controlled text.
        sentinel = synthetic_sentinel("BRACKET-TARGET")
        result = app.normalized_route("http://[" + sentinel + "::1/api/scenarios")
        self.assertEqual(result, "/{other}")
        self.assertNotIn(sentinel, result)


class RequestEventTests(unittest.TestCase):
    """``event=request`` records carry only allowlisted, non-sensitive fields."""

    def emit(self, *args, **kwargs) -> dict:
        buf = io.StringIO()
        with redirect_stderr(buf):
            app.log_request_event(*args, **kwargs)
        recs = records_from(buf.getvalue())
        self.assertEqual(len(recs), 1, f"expected exactly one JSON line, got {buf.getvalue()!r}")
        return recs[0]

    def test_normal_request_event_shape(self) -> None:
        rec = self.emit("GET", "/readyz", 200, 3)
        self.assertEqual(rec["event"], "request")
        self.assertEqual(rec["level"], "info")
        self.assertEqual(rec["method"], "GET")
        self.assertEqual(rec["path"], "/readyz")
        self.assertEqual(rec["status"], 200)
        self.assertEqual(rec["elapsed_ms"], 3)
        self.assertIn("ts", rec)

    def test_keys_are_within_allowlist(self) -> None:
        rec = self.emit("POST", "/api/scenarios/{slug}/actions", 201, 5)
        self.assertLessEqual(set(rec), ALLOWED_REQUEST_KEYS)

    def test_elapsed_omitted_when_unknown(self) -> None:
        rec = self.emit("GET", "/{other}", 404, None)
        self.assertNotIn("elapsed_ms", rec)
        self.assertEqual(rec["status"], 404)

    def test_emitted_line_is_stderr_only(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            app.log_request_event("GET", "/readyz", 200, 1)
        self.assertEqual(out.getvalue(), "", "structured logs must not pollute stdout (SERVING channel)")
        self.assertTrue(records_from(err.getvalue()), "request event must be written to stderr")


class ErrorEventTests(unittest.TestCase):
    """``event=error`` records carry a stable category + class name, never text."""

    def emit(self, *args, **kwargs) -> tuple:
        buf = io.StringIO()
        with redirect_stderr(buf):
            app.log_error_event(*args, **kwargs)
        raw = buf.getvalue()
        recs = records_from(raw)
        self.assertEqual(len(recs), 1, f"expected exactly one JSON line, got {raw!r}")
        return recs[0], raw

    def test_error_event_shape(self) -> None:
        rec, _ = self.emit("GET", "/api/scenarios/{slug}", "database_error", RuntimeError("x"))
        self.assertEqual(rec["event"], "error")
        self.assertEqual(rec["level"], "error")
        self.assertEqual(rec["method"], "GET")
        self.assertEqual(rec["path"], "/api/scenarios/{slug}")
        self.assertEqual(rec["category"], "database_error")
        self.assertEqual(rec["exc_type"], "RuntimeError")
        self.assertEqual(rec["status"], 500)
        self.assertLessEqual(set(rec), ALLOWED_ERROR_KEYS)

    def test_raw_exception_message_is_never_logged(self) -> None:
        sentinel = synthetic_sentinel("EXCMSG")
        exc = RuntimeError("host=localhost password=" + sentinel + " sslmode=disable")
        rec, raw = self.emit("POST", "/api/scenarios/{slug}/actions", "internal_error", exc)
        # The class name is a safe diagnostic and must be present...
        self.assertEqual(rec["exc_type"], "RuntimeError")
        # ...but no fragment of str(exc) may appear anywhere in the output.
        self.assertNotIn(sentinel, raw)
        self.assertNotIn(str(exc), raw)
        self.assertNotIn("password=", raw)

    def test_sqlite_error_logs_class_name_not_detail(self) -> None:
        import sqlite3

        sentinel = synthetic_sentinel("SQLMSG")
        exc = sqlite3.OperationalError("no such table: " + sentinel)
        rec, raw = self.emit("GET", "/readyz", "database_error", exc)
        self.assertEqual(rec["exc_type"], "OperationalError")
        self.assertNotIn(sentinel, raw)
        self.assertNotIn("no such table", raw)

    def test_error_line_is_stderr_only(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            app.log_error_event("GET", "/readyz", "database_error", RuntimeError("x"))
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(records_from(err.getvalue()))


class MethodAllowlistTests(unittest.TestCase):
    """The logged HTTP method is a finite allowlist; caller-selected method
    tokens are never reproduced in the log context."""

    def test_standard_methods_pass_through(self) -> None:
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(app._allowed_method(method), method)

    def test_arbitrary_method_is_not_reproduced(self) -> None:
        arbitrary = "ZQ" + "SYNTHMETHOD"  # alphabetic, <=16-char, obviously synthetic
        self.assertEqual(app._allowed_method(arbitrary), "OTHER")
        self.assertNotIn(arbitrary, app._allowed_method(arbitrary))

    def test_nonstandard_shapes_fall_back(self) -> None:
        for bad in (None, "", "get", "post", 123, "GET ", "CONNECTX"):
            with self.subTest(value=bad):
                self.assertEqual(app._allowed_method(bad), "OTHER")


# ======================================================================
# Integration-level: a real localhost server, stderr captured to a file.
# ======================================================================


class _LiveServerCase(unittest.TestCase):
    """Base: reseed the synthetic DB, run app.py, capture stdout/stderr to files."""

    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run([sys.executable, str(SEED)], check=True, text=True, capture_output=True)
        cls.host = "127.0.0.1"
        cls.port = cls._free_port()
        cls.base = f"http://{cls.host}:{cls.port}"
        cls.origin = f"http://{cls.host}:{cls.port}"
        cls._out = tempfile.NamedTemporaryFile(mode="wb", suffix=".out", delete=False)
        cls._err = tempfile.NamedTemporaryFile(mode="wb", suffix=".err", delete=False)
        cls.out_path = Path(cls._out.name)
        cls.err_path = Path(cls._err.name)
        cls.proc = subprocess.Popen(
            [sys.executable, str(APP), "--host", cls.host, "--port", str(cls.port)],
            stdout=cls._out,
            stderr=cls._err,
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
        for handle in (cls._out, cls._err):
            try:
                handle.close()
            except OSError:
                pass
        for path in (cls.out_path, cls.err_path):
            try:
                path.unlink()
            except OSError:
                pass

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @classmethod
    def _wait_ready(cls) -> None:
        last = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"{cls.base}/readyz", timeout=RAW_TIMEOUT) as resp:
                    if json.loads(resp.read().decode("utf-8")).get("status") == "ok":
                        return
            except Exception as exc:  # noqa: BLE001 - surface final error after retries
                last = exc
                time.sleep(0.1)
        raise RuntimeError(f"demo API did not become ready: {last}")

    # -- capture helpers ---------------------------------------------------

    def stderr_text(self) -> str:
        return self.err_path.read_text(encoding="utf-8", errors="replace")

    def stderr_records(self) -> list:
        return records_from(self.stderr_text())

    def request(self, path: str, *, method: str = "GET", data: bytes = None, headers: dict = None) -> tuple:
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=RAW_TIMEOUT) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def raw_request(self, raw: bytes) -> tuple:
        """Send a fully caller-controlled request line; bounded so it can never
        hang. Returns (status_or_None, decoded_response_text)."""
        with socket.create_connection((self.host, self.port), timeout=RAW_TIMEOUT) as sock:
            sock.settimeout(RAW_TIMEOUT)
            sock.sendall(raw)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            chunks = []
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except socket.timeout:
                pass
        data = b"".join(chunks)
        status = None
        if data:
            fields = data.split(b"\r\n", 1)[0].split(b" ")
            if len(fields) >= 2 and fields[1].isdigit():
                status = int(fields[1])
        return status, data.decode("utf-8", "replace")


class LiveRequestLoggingTests(_LiveServerCase):
    def test_serving_banner_stays_on_stdout(self) -> None:
        banner = self.out_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("SERVING http://", banner, "the SERVING readiness signal must remain on stdout")
        self.assertEqual(records_from(banner), [], "structured JSON logs must not appear on stdout")

    def test_normal_request_emits_structured_event(self) -> None:
        status, _ = self.request("/readyz")
        self.assertEqual(status, 200)
        matches = [
            r for r in self.stderr_records()
            if r.get("event") == "request" and r.get("path") == "/readyz" and r.get("status") == 200
        ]
        self.assertTrue(matches, "a normal request must emit a structured request event")
        self.assertEqual(matches[-1]["method"], "GET")

    def test_client_rejection_emits_structured_event(self) -> None:
        # A cross-origin write is rejected 403; the rejection must be diagnosable
        # as a structured request event whose path is the collapsed route label.
        body = json.dumps({"action_type": "open_repair_task", "actor": "demo_operator"}).encode("utf-8")
        status, _ = self.request(
            f"/api/scenarios/{TARGET_SLUG}/actions",
            method="POST",
            data=body,
            headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        matches = [
            r for r in self.stderr_records()
            if r.get("event") == "request"
            and r.get("path") == "/api/scenarios/{slug}/actions"
            and r.get("status") == 403
        ]
        self.assertTrue(matches, "a client rejection must emit a structured request event")

    def test_not_found_is_logged_without_caller_path_text(self) -> None:
        sentinel = synthetic_sentinel("NOTFOUND-PATH")
        status, _ = self.request(f"/admin/{sentinel}")
        self.assertEqual(status, 404)
        self.assertNotIn(sentinel, self.stderr_text(), "unknown path text must not be logged")
        self.assertTrue(
            any(r.get("event") == "request" and r.get("status") == 404 for r in self.stderr_records())
        )

    def test_query_values_are_never_logged(self) -> None:
        sentinel = synthetic_sentinel("QUERY")
        status, _ = self.request(f"/api/scenarios?access_token={sentinel}&password={sentinel}")
        self.assertEqual(status, 200)
        self.assertNotIn(sentinel, self.stderr_text(), "query string values must never be logged")

    def test_header_values_are_never_logged(self) -> None:
        sentinel = synthetic_sentinel("HEADER")
        status, _ = self.request(
            "/readyz",
            headers={
                "Authorization": "Bearer " + sentinel,
                "Cookie": "session=" + sentinel,
                "X-Api-Key": sentinel,
            },
        )
        self.assertEqual(status, 200)
        self.assertNotIn(sentinel, self.stderr_text(), "authorization/cookie/api-key values must never be logged")

    def test_body_values_are_never_logged(self) -> None:
        sentinel = synthetic_sentinel("BODY")
        # Valid content-type + origin so the body is actually read/parsed, then
        # rejected 422 on the (sentinel) actor value: the body was consumed but
        # must not appear anywhere in the logs.
        body = json.dumps({"action_type": "open_repair_task", "actor": sentinel}).encode("utf-8")
        status, _ = self.request(
            f"/api/scenarios/{TARGET_SLUG}/actions",
            method="POST",
            data=body,
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        self.assertEqual(status, 422)
        self.assertNotIn(sentinel, self.stderr_text(), "request body field values must never be logged")

    def test_malformed_target_is_handled_without_traceback(self) -> None:
        # An unmatched IPv6 bracket in the request target makes urllib.parse
        # raise inside the handler. It must enter the safe structured internal-
        # error path -- deterministic 500, no traceback, no raw target text.
        sentinel = synthetic_sentinel("RAW-BRACKET-TARGET")
        before = len(self.stderr_text())
        raw = (
            "GET http://[" + sentinel + "::1/api/scenarios HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        status, _ = self.raw_request(raw)
        self.assertEqual(status, 500, "a malformed target must produce a deterministic 500")
        new_text = self.stderr_text()[before:]
        self.assertNotIn("Traceback", new_text, "a malformed target must not emit a framework traceback")
        self.assertNotIn(sentinel, new_text, "raw request-target text must never be logged")
        errors = [r for r in records_from(new_text) if r.get("event") == "error"]
        self.assertTrue(errors, "the malformed target must be logged as a structured error event")
        self.assertEqual(errors[-1]["category"], "internal_error")

    def test_arbitrary_method_not_reproduced_in_logs(self) -> None:
        arbitrary = "ZQ" + "SYNTHMETHOD"  # alphabetic, <=16 chars, obviously synthetic
        before = len(self.stderr_text())
        raw = (
            arbitrary + " /readyz HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        self.raw_request(raw)
        new_text = self.stderr_text()[before:]
        self.assertNotIn(arbitrary, new_text, "a caller-selected method token must not be reproduced in logs")
        matches = [
            r for r in records_from(new_text)
            if r.get("event") == "request" and r.get("method") == "OTHER"
        ]
        self.assertTrue(matches, "an unsupported method must log method 'OTHER'")

    def test_rejection_emits_structured_error_event(self) -> None:
        # A handled RequestRejected must emit a structured error event whose
        # category is the existing stable rejection code, exc_type is
        # RequestRejected, and status is the actual 4xx -- so same-status
        # rejection classes are individually diagnosable -- while body/header/
        # query values are still excluded.
        sentinel = synthetic_sentinel("REJECT-BODY")
        path = f"/api/scenarios/{TARGET_SLUG}/actions"
        json_headers = {"Content-Type": "application/json", "Origin": self.origin}
        cases = [
            # (headers, body, expected status, expected stable category)
            ({"Content-Type": "application/json", "Origin": "http://evil.example"},
             json.dumps({"action_type": "open_repair_task", "actor": "demo_operator"}).encode("utf-8"),
             403, "origin_not_allowed"),
            (json_headers, b"{not valid json", 400, "invalid_json"),
            (json_headers, b"[]", 400, "invalid_payload"),
            (json_headers,
             json.dumps({"action_type": "open_repair_task", "actor": sentinel}).encode("utf-8"),
             422, "invalid_actor"),
        ]
        seen_categories = set()
        for headers, body, want_status, want_category in cases:
            with self.subTest(category=want_category):
                before = len(self.stderr_text())
                status, _ = self.request(path, method="POST", data=body, headers=headers)
                self.assertEqual(status, want_status)
                new = records_from(self.stderr_text()[before:])
                errs = [r for r in new if r.get("event") == "error"]
                self.assertTrue(errs, f"{want_category}: rejection must emit a structured error event")
                err = errs[-1]
                self.assertEqual(err["category"], want_category)
                self.assertEqual(err["exc_type"], "RequestRejected")
                self.assertEqual(err["status"], want_status)
                seen_categories.add(err["category"])
        # Body value must never appear in any log line.
        self.assertNotIn(sentinel, self.stderr_text(), "rejected request body values must never be logged")
        # Two distinct 400 classes must be individually diagnosable.
        self.assertIn("invalid_json", seen_categories)
        self.assertIn("invalid_payload", seen_categories)


class ServerErrorLoggingTests(_LiveServerCase):
    """A real server-side sqlite failure must yield a deterministic 500 whose
    response and logs expose no raw exception detail -- only a stable category
    and the exception class name."""

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        # Restore the shared synthetic DB for any downstream suite/run.
        subprocess.run([sys.executable, str(SEED)], check=True, text=True, capture_output=True)

    def test_induced_server_error_is_deterministic_and_safe(self) -> None:
        before = len(self.stderr_text())
        # Remove the synthetic DB out from under the running server. The next
        # query hits a freshly (re)created empty database -> sqlite "no such
        # table" -> the handled sqlite3.Error path.
        try:
            app.DB_PATH.unlink()
        except OSError:
            pass

        status, body = self.request("/readyz")
        self.assertEqual(status, 500)

        # Response body is deterministic and carries no raw exception detail.
        parsed = json.loads(body)
        self.assertEqual(parsed.get("status"), "error")
        self.assertNotIn("no such table", body)
        self.assertNotIn("seed_meta", body)
        self.assertNotIn("Traceback", body)
        detail = json.dumps(parsed)
        self.assertNotIn("sqlite3", detail)

        new_text = self.stderr_text()[before:]
        errors = [r for r in records_from(new_text) if r.get("event") == "error"]
        self.assertTrue(errors, "a handled server error must emit a structured error event")
        err = errors[-1]
        self.assertEqual(err["category"], "database_error")
        self.assertIn("Error", err["exc_type"])  # e.g. OperationalError
        self.assertEqual(err["status"], 500)
        # No fragment of the underlying exception text may appear in any log line.
        self.assertNotIn("no such table", new_text)
        self.assertNotIn("seed_meta", new_text)
        # The access-log request event for the same failure is also present.
        self.assertTrue(
            any(r.get("event") == "request" and r.get("status") == 500 for r in records_from(new_text))
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
