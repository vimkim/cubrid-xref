"""Tests for bidirectional PR <-> JIRA resolution and bundling."""

from __future__ import annotations

import unittest

from cubrid_xref.models import CliError, JiraIssue, PullRequest, RelatedJiraIssue
from cubrid_xref.parse import classify_repo, extract_jira_keys_from_pr
from cubrid_xref.resolver import resolve_jira, resolve_pr


def make_pr(
    repo: str,
    number: int,
    title: str,
    *,
    body: str = "",
    state: str = "open",
    is_draft: bool = False,
    merged_at: str | None = None,
    base_ref: str = "develop",
    head_ref: str = "",
) -> PullRequest:
    primary, mentioned = extract_jira_keys_from_pr(title, body, head_ref)
    return PullRequest(
        repo=repo,
        number=number,
        title=title,
        url=f"https://github.com/{repo}/pull/{number}",
        state=state,
        is_draft=is_draft,
        merged_at=merged_at,
        base_ref=base_ref,
        head_ref=head_ref,
        author="vimkim",
        role=classify_repo(repo),
        primary_issues=tuple(primary),
        mentioned_issues=tuple(mentioned),
        body=body,
    )


class FakeGitHubClient:
    def __init__(
        self,
        pulls: dict[tuple[str, int], PullRequest] | None = None,
        by_head: dict[tuple[str, str], list[PullRequest]] | None = None,
        search_map: dict[str, list[PullRequest]] | None = None,
    ) -> None:
        self.pulls = {(r.lower(), n): pr for (r, n), pr in (pulls or {}).items()}
        self.by_head = {(r.lower(), h): prs for (r, h), prs in (by_head or {}).items()}
        self.search_map = search_map or {}

    def get_pull(self, repo: str, number: int) -> PullRequest:
        key = (repo.lower(), number)
        if key not in self.pulls:
            raise CliError("NOT_FOUND", f"Not found: {repo}#{number}", exit_code=1)
        return self.pulls[key]

    def list_pulls_by_head(self, repo: str, head: str) -> list[PullRequest]:
        return list(self.by_head.get((repo.lower(), head), []))

    def search_prs(self, query: str) -> list[PullRequest]:
        return list(self.search_map.get(query, []))


class FakeJiraClient:
    def __init__(
        self,
        issues: dict[str, tuple[JiraIssue, list[RelatedJiraIssue], list[tuple[str, int]]]] | None = None,
        errors: dict[str, CliError] | None = None,
    ) -> None:
        self.issues = issues or {}
        self.errors = errors or {}

    def get_issue(
        self,
        key: str,
        *,
        include_links: bool = True,
    ) -> tuple[JiraIssue, list[RelatedJiraIssue], list[tuple[str, int]]]:
        up = key.upper()
        if up in self.errors:
            raise self.errors[up]
        if up not in self.issues:
            raise CliError("NOT_FOUND", f"Not found: {up}", exit_code=1)
        iss, rel, rlinks = self.issues[up]
        return iss, rel, (rlinks if include_links else [])


class ResolverTests(unittest.TestCase):
    def test_pr_to_jira_engine_and_siblings(self) -> None:
        eng = make_pr("CUBRID/cubrid", 8693, "[CBRD-28257] Fix catalog", head_ref="cbrd-28257")
        tc_priv = make_pr(
            "CUBRID/cubrid-testcases-private-ex",
            5098,
            "[CBRD-28257] Add shell TC",
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

        res = resolve_pr("CUBRID/cubrid", 8693, gh, jira)
        self.assertEqual([i.key for i in res.primary_issues], ["CBRD-28257"])
        self.assertEqual(res.engine_pr.number, 8693)
        self.assertEqual([p.number for p in res.tc_private], [5098])
        self.assertEqual([p.number for p in res.tc_public], [4400])

        # Reverse from TC PR with no JIRA key in its own title -> inherits from engine PR
        tc_no_key = make_pr(
            "CUBRID/cubrid-testcases",
            4400,
            "Draft: TC changes for PR CUBRID/cubrid#8693",
            is_draft=True,
            head_ref="tc/pr-8693",
        )
        gh.pulls[("cubrid/cubrid-testcases", 4400)] = tc_no_key
        gh.by_head[("cubrid/cubrid-testcases", "CUBRID:tc/pr-8693")] = [tc_no_key]
        res_tc = resolve_pr("CUBRID/cubrid-testcases", 4400, gh, jira)
        self.assertEqual([i.key for i in res_tc.primary_issues], ["CBRD-28257"])
        self.assertEqual(res_tc.engine_pr.number, 8693)
        self.assertEqual([p.number for p in res_tc.tc_private], [5098])

    def test_pr_to_jira_multi_cbrd_and_jira_warning(self) -> None:
        eng = make_pr("CUBRID/cubrid", 9001, "[CBRD-100][CBRD-200] Joint fix")
        gh = FakeGitHubClient(pulls={("CUBRID/cubrid", 9001): eng})
        jira = FakeJiraClient(
            issues={
                "CBRD-100": (
                    JiraIssue(key="CBRD-100", url="https://jira.cubrid.org/browse/CBRD-100", summary="One", status="Open"),
                    [],
                    [],
                )
            },
            errors={
                "CBRD-200": CliError("UPSTREAM_UNAVAILABLE", "JIRA down", exit_code=75, transient=True),
            },
        )
        res = resolve_pr("CUBRID/cubrid", 9001, gh, jira)
        self.assertEqual([i.key for i in res.primary_issues], ["CBRD-100", "CBRD-200"])
        self.assertTrue(res.primary_issues[0].found)
        self.assertFalse(res.primary_issues[1].found)
        self.assertEqual(len(res.warnings), 1)

    def test_jira_to_pr_bundles_recreates_reverts_legacy_and_mentions(self) -> None:
        key = "CBRD-26357"
        eng_old = make_pr("CUBRID/cubrid", 6904, "[CBRD-26357] Draft tracking PR", state="closed", is_draft=True)
        eng_new = make_pr("CUBRID/cubrid", 7990, "[CBRD-26357] Add OOS", state="closed", merged_at="2026-11-19T15:00:00Z")
        eng_legacy = make_pr("CUBRID/cubrid", 3000, "[CBRD-26357] Legacy prep", state="closed", merged_at="2023-01-01T00:00:00Z")
        noisy_mention = make_pr(
            "CUBRID/cubrid",
            8685,
            "[CBRD-27527] Unrelated PR",
            body="See https://jira.cubrid.org/browse/CBRD-26357 in CI analysis",
        )

        tc_priv_1 = make_pr(
            "CUBRID/cubrid-testcases-private-ex",
            4210,
            "[CBRD-26357] Add shell TC",
            state="closed",
            merged_at="2026-11-19T04:00:00Z",
            head_ref="tc/pr-7990",
        )
        tc_priv_revert = make_pr(
            "CUBRID/cubrid-testcases-private-ex",
            5014,
            'Revert "[CBRD-26357] Add shell TC (#4210)"',
            state="closed",
            merged_at="2026-11-19T04:41:00Z",
            head_ref="revert-4210-tc/pr-7990",
        )
        tc_priv_2 = make_pr(
            "CUBRID/cubrid-testcases-private-ex",
            5015,
            "[CBRD-26357] TC changes for PR CUBRID/cubrid#7990",
            state="closed",
            merged_at="2026-11-19T15:06:00Z",
            head_ref="tc/pr-7990",
        )
        tc_pub_draft_closed = make_pr(
            "CUBRID/cubrid-testcases",
            3876,
            "[CBRD-26357] Draft: TC changes for PR CUBRID/cubrid#7990",
            state="closed",
            is_draft=True,
            head_ref="tc/pr-7990",
        )
        tc_ha_standalone = make_pr(
            "CUBRID/cubrid-testcases-private",
            1701,
            "[CBRD-26357] Exclude HA overflow case",
            state="closed",
            merged_at="2026-11-24T04:44:00Z",
            head_ref="cbrd26357_exclude",
        )
        other_jdbc = make_pr("CUBRID/cubrid-jdbc", 12, "[CBRD-26357] JDBC support")

        # Search stubs (without head_ref) to mirror GitHub Search API behavior
        stub_revert = make_pr(
            "CUBRID/cubrid-testcases-private-ex",
            5014,
            'Revert "[CBRD-26357] Add shell TC (#4210)"',
            state="closed",
            merged_at="2026-11-19T04:41:00Z",
            head_ref="",
        )
        stub_ha = make_pr(
            "CUBRID/cubrid-testcases-private",
            1701,
            "[CBRD-26357] Exclude HA overflow case",
            state="closed",
            merged_at="2026-11-24T04:44:00Z",
            head_ref="",
        )

        q_title = f'org:CUBRID is:pr "{key}" in:title'
        q_body = f'org:CUBRID is:pr "browse/{key}" in:body'

        gh = FakeGitHubClient(
            pulls={
                ("CUBRID/cubrid", 3000): eng_legacy,
                ("CUBRID/cubrid", 6904): eng_old,
                ("CUBRID/cubrid", 7990): eng_new,
                ("CUBRID/cubrid-testcases-private-ex", 4210): tc_priv_1,
                ("CUBRID/cubrid-testcases-private-ex", 5014): tc_priv_revert,
                ("CUBRID/cubrid-testcases-private-ex", 5015): tc_priv_2,
                ("CUBRID/cubrid-testcases", 3876): tc_pub_draft_closed,
                ("CUBRID/cubrid-testcases-private", 1701): tc_ha_standalone,
                ("CUBRID/cubrid-jdbc", 12): other_jdbc,
            },
            by_head={
                ("CUBRID/cubrid-testcases-private-ex", "CUBRID:tc/pr-7990"): [tc_priv_1, tc_priv_2],
                ("CUBRID/cubrid-testcases", "CUBRID:tc/pr-7990"): [tc_pub_draft_closed],
            },
            search_map={
                q_title: [eng_old, eng_new, eng_legacy, stub_revert, stub_ha, other_jdbc],
                q_body: [noisy_mention],
            },
        )
        followup_eng = make_pr("CUBRID/cubrid", 8434, "[CBRD-27781] Followup fix")
        jira = FakeJiraClient(
            issues={
                key: (
                    JiraIssue(key=key, url=f"https://jira.cubrid.org/browse/{key}", summary="OOS", status="Develop"),
                    [
                        RelatedJiraIssue(
                            relation="relates to",
                            key="CBRD-27781",
                            url="https://jira.cubrid.org/browse/CBRD-27781",
                            summary="Followup",
                            status="Resolved",
                        )
                    ],
                    [],
                ),
                "CBRD-27781": (
                    JiraIssue(key="CBRD-27781", url="https://jira.cubrid.org/browse/CBRD-27781", summary="Followup", status="Resolved"),
                    [],
                    [],
                ),
            }
        )
        gh.search_map['org:CUBRID is:pr "CBRD-27781" in:title'] = [followup_eng]
        gh.pulls[("cubrid/cubrid", 8434)] = followup_eng

        # Default: excludes mentions and does not follow links
        res = resolve_jira(key, gh, jira)
        self.assertEqual([b.engine.number for b in res.bundles], [3000, 6904, 7990])
        self.assertEqual(res.bundles[0].tc_private, ())
        self.assertEqual(res.bundles[0].tc_public, ())
        self.assertEqual([p.number for p in res.bundles[2].tc_private], [4210, 5014, 5015])
        self.assertEqual([p.number for p in res.bundles[2].tc_public], [3876])
        self.assertEqual([p.number for p in res.standalone_tc_private], [1701])
        self.assertEqual([p.number for p in res.other_prs], [12])
        self.assertEqual(res.mentions, ())
        self.assertEqual(res.followups, ())

        # With include_mentions & follow_links
        res_full = resolve_jira(key, gh, jira, include_mentions=True, follow_links=True)
        self.assertEqual([p.number for p in res_full.mentions], [8685])
        self.assertEqual(len(res_full.followups), 1)
        self.assertEqual(res_full.followups[0].issue.key, "CBRD-27781")
        self.assertEqual([b.engine.number for b in res_full.followups[0].bundles], [8434])


if __name__ == "__main__":
    unittest.main()
