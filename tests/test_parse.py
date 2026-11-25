"""Tests for target parsing, repo classification, and PR/JIRA reference extraction."""

from __future__ import annotations

import unittest

from cubrid_xref.models import CliError
from cubrid_xref.parse import (
    classify_repo,
    extract_engine_pr_refs_from_tc_pr,
    extract_jira_keys_from_pr,
    extract_reverted_pr_number,
    parse_target,
)


class ParseTargetTests(unittest.TestCase):
    def test_github_pr_urls(self) -> None:
        t = parse_target("https://github.com/CUBRID/cubrid/pull/8693")
        self.assertEqual((t.kind, t.repo, t.number), ("pr", "CUBRID/cubrid", 8693))

        t2 = parse_target("https://github.com/CUBRID/cubrid-testcases-private-ex/pull/5098/files#diff-1")
        self.assertEqual(
            (t2.kind, t2.repo, t2.number),
            ("pr", "CUBRID/cubrid-testcases-private-ex", 5098),
        )

    def test_repo_shorthand_refs(self) -> None:
        t = parse_target("cubrid-testcases#4400")
        self.assertEqual((t.kind, t.repo, t.number), ("pr", "CUBRID/cubrid-testcases", 4400))

        t2 = parse_target("CUBRID/cubrid-testcases-private#1697")
        self.assertEqual(
            (t2.kind, t2.repo, t2.number),
            ("pr", "CUBRID/cubrid-testcases-private", 1697),
        )

    def test_bare_pr_refs(self) -> None:
        for raw in ("8693", "#8693", "pr-8693", "PR-8693"):
            t = parse_target(raw)
            self.assertEqual((t.kind, t.repo, t.number), ("pr", "CUBRID/cubrid", 8693))

        t_custom = parse_target("4400", default_repo="CUBRID/cubrid-testcases")
        self.assertEqual((t_custom.kind, t_custom.repo, t_custom.number), ("pr", "CUBRID/cubrid-testcases", 4400))

    def test_jira_keys_and_urls(self) -> None:
        t = parse_target("cbrd-28257")
        self.assertEqual((t.kind, t.issue_key), ("jira", "CBRD-28257"))

        t2 = parse_target("LETS-289")
        self.assertEqual((t2.kind, t2.issue_key), ("jira", "LETS-289"))

        t3 = parse_target("https://jira.cubrid.org/browse/CBRD-28257")
        self.assertEqual((t3.kind, t3.issue_key, t3.jira_server), ("jira", "CBRD-28257", "https://jira.cubrid.org"))

        t4 = parse_target("https://jira.cubrid.com/browse/CBRD-12345?focusedCommentId=1")
        self.assertEqual((t4.kind, t4.issue_key, t4.jira_server), ("jira", "CBRD-12345", "https://jira.cubrid.com"))

    def test_forced_kind(self) -> None:
        t = parse_target("28257", forced_kind="jira")
        self.assertEqual((t.kind, t.issue_key), ("jira", "CBRD-28257"))

        with self.assertRaises(CliError) as ctx:
            parse_target("CBRD-28257", forced_kind="pr")
        self.assertEqual(ctx.exception.exit_code, 2)

        with self.assertRaises(CliError) as ctx2:
            parse_target("https://github.com/CUBRID/cubrid/pull/8693", forced_kind="jira")
        self.assertEqual(ctx2.exception.exit_code, 2)

    def test_invalid_targets(self) -> None:
        for bad in ("", "   ", "UTF-8", "SHA-256", "not-a-target"):
            with self.assertRaises(CliError) as ctx:
                parse_target(bad)
            self.assertEqual(ctx.exception.exit_code, 2)


class ClassifyRepoTests(unittest.TestCase):
    def test_roles(self) -> None:
        self.assertEqual(classify_repo("CUBRID/cubrid"), "engine")
        self.assertEqual(classify_repo("cubrid/cubrid"), "engine")
        self.assertEqual(classify_repo("CUBRID/cubrid-testcases"), "tc_public")
        self.assertEqual(classify_repo("CUBRID/cubrid-testcases-private-ex"), "tc_private")
        self.assertEqual(classify_repo("CUBRID/cubrid-testcases-private"), "tc_private")
        self.assertEqual(classify_repo("CUBRID/cubrid-jdbc"), "other")


class ExtractRefsTests(unittest.TestCase):
    def test_single_and_multi_cbrd_extraction(self) -> None:
        p1, m1 = extract_jira_keys_from_pr(
            "[CBRD-28257] Fix catalog padding",
            "<http://jira.cubrid.org/browse/CBRD-28257>\n\nShell TC `cbrd_27780` failed. Also see CBRD-27780 and UTF-8 / SHA-256 / PR-999.",
        )
        self.assertEqual(p1, ["CBRD-28257"])
        self.assertEqual(m1, ["CBRD-27780"])

        p2, m2 = extract_jira_keys_from_pr(
            "[CBRD-28019][CBRD-28153] Fix two issues",
            "Body with CBRD-28019 and LETS-289",
        )
        self.assertEqual(p2, ["CBRD-28019", "CBRD-28153"])
        self.assertEqual(m2, ["LETS-289"])

        p3, _ = extract_jira_keys_from_pr(
            "[CBRD-28019, CBRD-28153] Combined fix",
            "",
        )
        self.assertEqual(p3, ["CBRD-28019", "CBRD-28153"])

    def test_top_body_and_head_fallback(self) -> None:
        p1, _ = extract_jira_keys_from_pr(
            "Title without key",
            "\nhttp://jira.cubrid.org/browse/CBRD-24330\nDetails...",
        )
        self.assertEqual(p1, ["CBRD-24330"])

        p2, _ = extract_jira_keys_from_pr(
            "Title without key",
            "No jira link in body",
            head_ref="feature/cbrd-27443-fix",
        )
        self.assertEqual(p2, ["CBRD-27443"])

        # Should not match lowercase cbrd_27780 path in body as a key
        p3, m3 = extract_jira_keys_from_pr(
            "[CBRD-28257] Fix",
            "Failed in shell/_06_issues/_26_2h/cbrd_27780",
        )
        self.assertEqual(p3, ["CBRD-28257"])
        self.assertEqual(m3, [])

    def test_tc_engine_pr_extraction(self) -> None:
        self.assertEqual(
            extract_engine_pr_refs_from_tc_pr(
                "[CBRD-28257] Draft: TC changes for PR CUBRID/cubrid#8693",
                "Related Engine PR: CUBRID/cubrid#8693",
                "tc/pr-8693",
            ),
            [8693],
        )
        self.assertEqual(
            extract_engine_pr_refs_from_tc_pr(
                "Revert \"[CBRD-26357] Add shell testcases (#4210)\"",
                "",
                "revert-4210-tc/pr-7990",
            ),
            [7990],
        )
        self.assertEqual(
            extract_reverted_pr_number('Revert "[CBRD-26357] Add shell testcases (#4210)"'),
            4210,
        )


if __name__ == "__main__":
    unittest.main()
