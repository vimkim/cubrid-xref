"""Tests for CLI flag parsing, output modes, stream separation, and exit codes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cubrid_xref.cache import XrefCache
from cubrid_xref.cli import main
from cubrid_xref.formatters import resolve_output_mode
from cubrid_xref.models import CliError, JiraIssue
from tests.test_resolver import FakeGitHubClient, FakeJiraClient, make_pr


class OutputModeTests(unittest.TestCase):
    def test_precedence(self) -> None:
        self.assertEqual(
            resolve_output_mode(
                json_flag=True,
                plain_flag=False,
                no_color_flag=False,
                stdout_is_tty=True,
                env={"FORCE_COLOR": "1"},
            ).fmt,
            "json",
        )
        m_nc = resolve_output_mode(
            json_flag=False,
            plain_flag=False,
            no_color_flag=False,
            stdout_is_tty=True,
            env={"NO_COLOR": "1"},
        )
        self.assertFalse(m_nc.color)

        m_fc = resolve_output_mode(
            json_flag=False,
            plain_flag=False,
            no_color_flag=False,
            stdout_is_tty=False,
            env={"NO_COLOR": "1", "FORCE_COLOR": "1"},
        )
        self.assertTrue(m_fc.color)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_dir = Path(tmp.name)

    def _factories(self):
        eng = make_pr("CUBRID/cubrid", 8693, "[CBRD-28257] Catalog fix", head_ref="cbrd-28257")
        tc_priv = make_pr("CUBRID/cubrid-testcases-private-ex", 5098, "[CBRD-28257] Shell TC", head_ref="tc/pr-8693")
        tc_pub = make_pr("CUBRID/cubrid-testcases", 4400, "[CBRD-28257] Draft: TC", is_draft=True, head_ref="tc/pr-8693")
        gh = FakeGitHubClient(
            pulls={
                ("CUBRID/cubrid", 8693): eng,
                ("CUBRID/cubrid-testcases-private-ex", 5098): tc_priv,
                ("CUBRID/cubrid-testcases", 4400): tc_pub,
            },
            by_head={
                ("CUBRID/cubrid-testcases-private-ex", "CUBRID:tc/pr-8693"): [tc_priv],
                ("CUBRID/cubrid-testcases", "CUBRID:tc/pr-8693"): [tc_pub],
            },
            search_map={
                'org:CUBRID is:pr "CBRD-28257" in:title': [eng, tc_priv, tc_pub],
                'org:CUBRID is:pr "browse/CBRD-28257" in:body': [],
            },
        )
        jira = FakeJiraClient(
            issues={
                "CBRD-28257": (
                    JiraIssue(
                        key="CBRD-28257",
                        url="https://jira.cubrid.org/browse/CBRD-28257",
                        summary="Catalog fix",
                        status="Develop",
                    ),
                    [],
                    [],
                )
            }
        )
        cache_path = self.cache_dir / "cache.sqlite3"
        return (lambda **_: gh), (lambda **_: jira), (lambda _p: XrefCache(cache_path))

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        gh_f, jira_f, cache_f = self._factories()
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = main(argv, gh_factory=gh_f, jira_factory=jira_f, cache_factory=cache_f)
        return code, out.getvalue(), err.getvalue()

    def test_flag_orderings_and_json_envelope(self) -> None:
        for argv in (
            ["--json", "8693"],
            ["8693", "--json"],
            ["pr", "8693", "--json"],
            ["--json", "pr", "8693"],
        ):
            code, stdout, stderr = self._run(argv)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["data"]["kind"], "pr")
            self.assertEqual(payload["data"]["primary_issues"][0]["key"], "CBRD-28257")
            self.assertIn("cache", payload["data"])

        for argv in (
            ["jira", "28257", "--json"],
            ["--json", "jira", "CBRD-28257"],
            ["CBRD-28257", "--json"],
        ):
            code, stdout, stderr = self._run(argv)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["kind"], "jira")
            self.assertEqual(payload["data"]["bundles"][0]["engine"]["number"], 8693)

    def test_plain_and_human_no_ansi(self) -> None:
        code, stdout, stderr = self._run(["--plain", "CBRD-28257"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("\033[", stdout)
        self.assertIn("BUNDLE_1_ENGINE\tCUBRID/cubrid#8693\tOPEN\t", stdout)
        self.assertIn("BUNDLE_1_TC_PRIVATE\tCUBRID/cubrid-testcases-private-ex#5098\t", stdout)
        self.assertIn("BUNDLE_1_TC_PUBLIC\tCUBRID/cubrid-testcases#4400\tDRAFT\t", stdout)

    def test_json_error_goes_to_stderr_only(self) -> None:
        code, stdout, stderr = self._run(["--json", "CBRD-999999"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        err_payload = json.loads(stderr.strip().splitlines()[-1])
        self.assertFalse(err_payload["ok"])
        self.assertEqual(err_payload["error"]["code"], "NOT_FOUND")

        code2, stdout2, stderr2 = self._run(["--json"])
        self.assertEqual(code2, 2)
        self.assertEqual(stdout2, "")
        err_payload2 = json.loads(stderr2.strip().splitlines()[-1])
        self.assertEqual(err_payload2["error"]["code"], "INVALID_USAGE")

    def test_conflicting_cache_flags(self) -> None:
        for argv in (
            ["--refresh", "--cache-only", "8693"],
            ["--refresh", "--no-cache", "8693"],
            ["--cache-only", "--no-cache", "8693"],
            ["--ttl", "-1", "8693"],
            ["--expired", "8693"],
        ):
            code, stdout, stderr = self._run(argv + ["--json"])
            self.assertEqual(code, 2, f"Expected exit 2 for {argv}, got {code}: {stderr}")
            self.assertEqual(stdout, "")
            self.assertEqual(json.loads(stderr)["error"]["code"], "INVALID_USAGE")

    def test_transient_and_auth_exit_codes(self) -> None:
        def bad_auth_gh(**_):
            class _GH(FakeGitHubClient):
                def get_pull(self, repo: str, number: int):
                    raise CliError("AUTH_MISSING", "No token", exit_code=78)
            return _GH()

        def rate_gh(**_):
            class _GH(FakeGitHubClient):
                def get_pull(self, repo: str, number: int):
                    raise CliError("RATE_LIMITED", "Rate limited", exit_code=75, transient=True)
            return _GH()

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--no-cache", "8693"], gh_factory=bad_auth_gh, jira_factory=lambda **_: FakeJiraClient())
        self.assertEqual(code, 78)
        self.assertEqual(out.getvalue(), "")

        out2 = io.StringIO()
        err2 = io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            code2 = main(["--no-cache", "8693", "--json"], gh_factory=rate_gh, jira_factory=lambda **_: FakeJiraClient())
        self.assertEqual(code2, 75)
        self.assertEqual(out2.getvalue(), "")
        self.assertTrue(json.loads(err2.getvalue())["error"]["transient"])


if __name__ == "__main__":
    unittest.main()
