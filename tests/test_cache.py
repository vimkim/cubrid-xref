"""Tests for XDG SQLite caching, TTL expiry, refresh, cross-population, and CLI cache commands."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cubrid_xref.cache import DEFAULT_TTL_SECONDS, XrefCache
from cubrid_xref.cli import main
from cubrid_xref.models import CliError, JiraIssue, RelatedJiraIssue
from cubrid_xref.resolver import resolve_jira, resolve_pr
from tests.test_resolver import FakeGitHubClient, FakeJiraClient, make_pr


class CountingGitHubClient(FakeGitHubClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, object]] = []

    def get_pull(self, repo: str, number: int):
        self.calls.append(("get_pull", (repo.lower(), number)))
        return super().get_pull(repo, number)

    def list_pulls_by_head(self, repo: str, head: str):
        self.calls.append(("list_pulls_by_head", (repo.lower(), head)))
        return super().list_pulls_by_head(repo, head)

    def search_prs(self, query: str):
        self.calls.append(("search_prs", query))
        return super().search_prs(query)


class CountingJiraClient(FakeJiraClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def get_issue(self, key: str, *, include_links: bool = True):
        self.calls.append(key.upper())
        return super().get_issue(key, include_links=include_links)


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_dir = Path(tmp.name) / "cubrid-xref"
        self.cache = XrefCache(self.cache_dir / "cache.sqlite3")

    def _build_clients(self):
        eng = make_pr("CUBRID/cubrid", 8693, "[CBRD-28257] Catalog fix", head_ref="cbrd-28257")
        tc_priv = make_pr(
            "CUBRID/cubrid-testcases-private-ex",
            5098,
            "[CBRD-28257] Shell TC",
            body="Engine PR: CUBRID/cubrid#8693",
            head_ref="tc/pr-8693",
        )
        tc_pub = make_pr(
            "CUBRID/cubrid-testcases",
            4400,
            "[CBRD-28257] Draft: TC changes for PR CUBRID/cubrid#8693",
            is_draft=True,
            head_ref="tc/pr-8693",
        )
        multi_eng = make_pr("CUBRID/cubrid", 9001, "[CBRD-28257][CBRD-99999] Joint fix")
        followup_eng = make_pr("CUBRID/cubrid", 8402, "[CBRD-27780] Follow-up fix")

        gh = CountingGitHubClient(
            pulls={
                ("CUBRID/cubrid", 8693): eng,
                ("CUBRID/cubrid", 9001): multi_eng,
                ("CUBRID/cubrid", 8402): followup_eng,
                ("CUBRID/cubrid-testcases-private-ex", 5098): tc_priv,
                ("CUBRID/cubrid-testcases", 4400): tc_pub,
            },
            by_head={
                ("CUBRID/cubrid-testcases-private-ex", "CUBRID:tc/pr-8693"): [tc_priv],
                ("CUBRID/cubrid-testcases", "CUBRID:tc/pr-8693"): [tc_pub],
            },
            search_map={
                'org:CUBRID is:pr "CBRD-28257" in:title': [eng, tc_priv, tc_pub, multi_eng],
                'org:CUBRID is:pr "browse/CBRD-28257" in:body': [],
                'org:CUBRID is:pr "CBRD-27780" in:title': [followup_eng],
                'org:CUBRID is:pr "browse/CBRD-27780" in:body': [],
            },
        )
        jira = CountingJiraClient(
            issues={
                "CBRD-28257": (
                    JiraIssue(
                        key="CBRD-28257",
                        url="https://jira.cubrid.org/browse/CBRD-28257",
                        summary="Catalog fix",
                        status="Develop",
                    ),
                    [
                        RelatedJiraIssue(
                            relation="relates to",
                            key="CBRD-27780",
                            url="https://jira.cubrid.org/browse/CBRD-27780",
                            summary="TDE",
                            status="Resolved",
                        )
                    ],
                    [],
                ),
                "CBRD-27780": (
                    JiraIssue(
                        key="CBRD-27780",
                        url="https://jira.cubrid.org/browse/CBRD-27780",
                        summary="TDE",
                        status="Resolved",
                    ),
                    [],
                    [],
                ),
            }
        )
        return gh, jira

    def test_pr_hit_ttl_expiry_refresh_and_offline(self) -> None:
        gh, jira = self._build_clients()
        t0 = 1_700_000_000.0

        res1 = resolve_pr("CUBRID/cubrid", 8693, gh, jira, cache=self.cache, now=t0)
        self.assertIsNotNone(res1.cache)
        self.assertFalse(res1.cache.hit)
        self.assertEqual(res1.cache.source, "live")
        self.assertGreater(len(gh.calls), 0)
        self.assertEqual(jira.calls, ["CBRD-28257"])

        # Verify restrictive permissions on directory (0700) and file (0600)
        self.assertEqual(stat.S_IMODE(self.cache_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.cache.path.stat().st_mode), 0o600)

        # Second call within 24h hits cache with 0 API calls
        gh.calls.clear()
        jira.calls.clear()
        res2 = resolve_pr("CUBRID/cubrid", 8693, gh, jira, cache=self.cache, now=t0 + 3600)
        self.assertTrue(res2.cache.hit)
        self.assertFalse(res2.cache.stale)
        self.assertEqual(res2.cache.source, "cache")
        self.assertEqual(res2.cache.age_seconds, 3600.0)
        self.assertEqual(gh.calls, [])
        self.assertEqual(jira.calls, [])
        self.assertEqual(res2.primary_issues[0].key, "CBRD-28257")
        self.assertEqual([p.number for p in res2.tc_private], [5098])

        # Offline stale read succeeds after 24h when cache_only=True
        res_stale = resolve_pr(
            "CUBRID/cubrid",
            8693,
            gh,
            jira,
            cache=self.cache,
            cache_only=True,
            now=t0 + DEFAULT_TTL_SECONDS + 10.0,
        )
        self.assertTrue(res_stale.cache.hit)
        self.assertTrue(res_stale.cache.stale)
        self.assertEqual(res_stale.cache.source, "cache_stale")
        self.assertEqual(gh.calls, [])

        # Online after 24h expires and re-fetches live
        res_expired = resolve_pr(
            "CUBRID/cubrid",
            8693,
            gh,
            jira,
            cache=self.cache,
            now=t0 + DEFAULT_TTL_SECONDS + 10.0,
        )
        self.assertFalse(res_expired.cache.hit)
        self.assertEqual(res_expired.cache.source, "live")
        self.assertGreater(len(gh.calls), 0)

        # Explicit refresh forces live fetch even when fresh
        gh.calls.clear()
        jira.calls.clear()
        res_ref = resolve_pr(
            "CUBRID/cubrid",
            8693,
            gh,
            jira,
            cache=self.cache,
            refresh=True,
            now=t0 + DEFAULT_TTL_SECONDS + 20.0,
        )
        self.assertFalse(res_ref.cache.hit)
        self.assertEqual(res_ref.cache.source, "live_refreshed")
        self.assertGreater(len(gh.calls), 0)

        # Offline miss raises CACHE_MISS
        with self.assertRaises(CliError) as ctx:
            resolve_pr("CUBRID/cubrid", 123456, gh, jira, cache=self.cache, cache_only=True, now=t0)
        self.assertEqual(ctx.exception.code, "CACHE_MISS")

    def test_jira_cross_population_and_followup_caching(self) -> None:
        gh, jira = self._build_clients()
        t0 = 1_700_000_000.0

        res = resolve_jira(
            "CBRD-28257",
            gh,
            jira,
            follow_links=True,
            cache=self.cache,
            now=t0,
        )
        self.assertFalse(res.cache.hit)
        self.assertEqual(len(res.followups), 1)
        self.assertEqual(res.followups[0].issue.key, "CBRD-27780")

        # Single-issue PRs in bundle were cross-populated -> 0 API calls for PR lookups
        gh.calls.clear()
        jira.calls.clear()
        for repo, num in (
            ("CUBRID/cubrid", 8693),
            ("CUBRID/cubrid-testcases-private-ex", 5098),
            ("CUBRID/cubrid-testcases", 4400),
        ):
            pr_res = resolve_pr(repo, num, gh, jira, cache=self.cache, now=t0 + 60)
            self.assertTrue(pr_res.cache.hit, f"Expected cross-populated hit for {repo}#{num}")
        self.assertEqual(gh.calls, [])
        self.assertEqual(jira.calls, [])

        # Follow-up JIRA issue was also cached individually
        rel_res = resolve_jira("CBRD-27780", gh, jira, cache=self.cache, now=t0 + 60)
        self.assertTrue(rel_res.cache.hit)
        self.assertEqual(gh.calls, [])

        # Multi-CBRD PR #9001 must NOT be cross-populated (would miss CBRD-99999 metadata)
        with self.assertRaises(CliError) as ctx:
            resolve_pr("CUBRID/cubrid", 9001, gh, jira, cache=self.cache, cache_only=True, now=t0 + 60)
        self.assertEqual(ctx.exception.code, "CACHE_MISS")

    def test_degraded_warnings_are_not_cached(self) -> None:
        gh, jira = self._build_clients()
        jira.errors["CBRD-28257"] = CliError(
            "UPSTREAM_UNAVAILABLE",
            "JIRA temporary error",
            exit_code=75,
            transient=True,
        )
        res = resolve_pr("CUBRID/cubrid", 8693, gh, jira, cache=self.cache, now=1_700_000_000.0)
        self.assertTrue(res.warnings)
        self.assertEqual(self.cache.stats()["total_entries"], 0)

    def test_cli_cache_status_and_clear(self) -> None:
        gh, jira = self._build_clients()
        t0 = time.time()
        resolve_pr("CUBRID/cubrid", 8693, gh, jira, cache=self.cache, now=t0)
        self.cache.put("jira", "CBRD-1", "v", {"kind": "jira", "issue": {"key": "CBRD-1", "url": ""}}, fetched_at=t0 - 200_000)

        def run_cli(argv: list[str]) -> tuple[int, str, str]:
            out = io.StringIO()
            err = io.StringIO()
            with (
                patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = main(
                    argv + ["--cache-dir", str(self.cache_dir)],
                    gh_factory=lambda **_: gh,
                    jira_factory=lambda **_: jira,
                )
            return code, out.getvalue(), err.getvalue()

        code, stdout, stderr = run_cli(["cache", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        data = json.loads(stdout)["data"]
        self.assertEqual(data["total_entries"], 2)

        code2, stdout2, _ = run_cli(["cache", "clear", "--expired", "--json"])
        self.assertEqual(code2, 0)
        data2 = json.loads(stdout2)["data"]
        self.assertEqual(data2["removed"], 1)
        self.assertEqual(data2["remaining"], 1)

        code3, stdout3, _ = run_cli(["cache", "clear", "--json"])
        self.assertEqual(code3, 0)
        data3 = json.loads(stdout3)["data"]
        self.assertEqual(data3["removed"], 1)
        self.assertEqual(data3["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
