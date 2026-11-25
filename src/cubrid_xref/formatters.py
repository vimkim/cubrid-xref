"""Output mode detection and human/plain/JSON formatters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from cubrid_xref.models import (
    CacheMeta,
    CliError,
    JiraIssue,
    JiraLookupResult,
    PrBundle,
    PrLookupResult,
    PullRequest,
)

SCHEMA_VERSION = 1

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_CYAN = "\033[36m"
_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_RED = "\033[31m"
_ANSI_MAGENTA = "\033[35m"


@dataclass(frozen=True)
class OutputMode:
    fmt: str  # "human" | "plain" | "json"
    color: bool


def resolve_output_mode(
    *,
    json_flag: bool,
    plain_flag: bool,
    no_color_flag: bool,
    stdout_is_tty: bool,
    env: Mapping[str, str],
) -> OutputMode:
    """Resolve output format and color mode once at the CLI boundary."""
    if json_flag:
        return OutputMode(fmt="json", color=False)
    if plain_flag:
        return OutputMode(fmt="plain", color=False)
    if no_color_flag:
        return OutputMode(fmt="human", color=False)

    force_color = env.get("FORCE_COLOR")
    if force_color is not None:
        enabled = force_color in ("", "1", "2", "3", "true")
        return OutputMode(fmt="human", color=enabled)

    if env.get("NO_COLOR") or "NODE_DISABLE_COLORS" in env:
        return OutputMode(fmt="human", color=False)

    if env.get("TERM") == "dumb":
        return OutputMode(fmt="human", color=False)

    if not stdout_is_tty:
        return OutputMode(fmt="human", color=False)

    return OutputMode(fmt="human", color=True)


def _c(text: str, code: str, *, color: bool) -> str:
    if not color:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def _format_age(seconds: float) -> str:
    secs = max(0, int(round(seconds)))
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _format_ttl(seconds: float) -> str:
    hours = seconds / 3600.0
    if abs(hours - round(hours)) < 1e-6:
        return f"{int(round(hours))}h"
    return f"{hours:.1f}h"


def _format_cache_line(cache: CacheMeta, *, color: bool) -> str:
    ttl_str = _format_ttl(cache.ttl_seconds)
    if cache.source == "cache":
        age_str = _format_age(cache.age_seconds)
        msg = f"Cache : hit (updated {age_str} ago, TTL {ttl_str}; pass --refresh to update)"
    elif cache.source == "cache_stale":
        age_str = _format_age(cache.age_seconds)
        msg = f"Cache : stale hit (updated {age_str} ago, TTL {ttl_str}; offline)"
    elif cache.source == "live_refreshed":
        msg = f"Cache : refreshed (saved; TTL {ttl_str})"
    elif cache.source == "live":
        msg = f"Cache : live (saved; TTL {ttl_str})"
    else:
        msg = "Cache : bypassed (--no-cache)"
    return _c(msg, _ANSI_DIM, color=color)


def _state_badge(pr: PullRequest, *, color: bool) -> str:
    label = f"[{pr.display_state}]"
    if not color:
        return label
    st = pr.display_state
    if st == "MERGED":
        return _c(label, _ANSI_MAGENTA, color=True)
    if st == "OPEN":
        return _c(label, _ANSI_GREEN, color=True)
    if st == "DRAFT":
        return _c(label, _ANSI_YELLOW, color=True)
    return _c(label, _ANSI_DIM, color=True)


def _format_pr_line(
    pr: PullRequest,
    *,
    color: bool,
    current_issue_key: str | None = None,
) -> str:
    base_part = f" (-> {pr.base_ref})" if pr.base_ref else ""
    extra_keys = [
        k for k in pr.primary_issues if current_issue_key is None or k != current_issue_key
    ]
    also_part = f" (also: {', '.join(extra_keys)})" if (current_issue_key and extra_keys) else ""
    ref = _c(pr.short_ref, _ANSI_BOLD, color=color)
    badge = _state_badge(pr, color=color)
    return f"{ref} {badge}{base_part} {pr.title}{also_part}\n  {pr.url}"


def _format_jira_line(issue: JiraIssue, *, color: bool) -> str:
    key_str = _c(issue.key, _ANSI_BOLD + _ANSI_CYAN, color=color)
    if not issue.found:
        return f"{key_str} (JIRA metadata unavailable)\n  {issue.url}"
    parts = [f"{key_str} [{issue.status}]"]
    if issue.issue_type:
        parts.append(f"({issue.issue_type})")
    if issue.summary:
        parts.append(issue.summary)
    line1 = " ".join(parts)
    meta: list[str] = []
    if issue.assignee:
        meta.append(f"assignee: {issue.assignee}")
    if issue.fix_versions:
        meta.append(f"target: {', '.join(issue.fix_versions)}")
    meta_str = f"  ({', '.join(meta)})" if meta else ""
    return f"{line1}\n  {issue.url}{meta_str}"


def format_pr_human(result: PrLookupResult, *, color: bool = False) -> str:
    lines: list[str] = []
    pr = result.target_pr
    header = _c(f"PR {pr.short_ref}", _ANSI_BOLD, color=color)
    base_part = f" (-> {pr.base_ref})" if pr.base_ref else ""
    lines.append(f"{header} {_state_badge(pr, color=color)}{base_part}")
    lines.append(f"Title : {pr.title}")
    lines.append(f"URL   : {pr.url}")
    if pr.head_ref:
        lines.append(f"Branch: {pr.head_ref}")
    if pr.author:
        lines.append(f"Author: {pr.author}")

    lines.append("")
    lines.append(_c("Linked JIRA Issues:", _ANSI_BOLD, color=color))
    if result.primary_issues:
        for issue in result.primary_issues:
            lines.append("  " + _format_jira_line(issue, color=color).replace("\n  ", "\n    "))
    else:
        lines.append("  (none)")

    if result.mentioned_issues:
        lines.append("")
        lines.append(_c("Mentioned JIRA Issues:", _ANSI_BOLD, color=color))
        for issue in result.mentioned_issues:
            lines.append("  " + _format_jira_line(issue, color=color).replace("\n  ", "\n    "))

    if result.engine_pr is not None or result.tc_private or result.tc_public:
        lines.append("")
        lines.append(_c("Sibling PRs:", _ANSI_BOLD, color=color))
        if result.engine_pr is not None:
            lines.append(
                "  engine    : "
                + _format_pr_line(result.engine_pr, color=color).replace("\n  ", "\n              ")
            )
        if result.tc_private:
            for idx, tc in enumerate(result.tc_private):
                prefix = "  tc-private: " if idx == 0 else "              "
                lines.append(
                    prefix + _format_pr_line(tc, color=color).replace("\n  ", "\n              ")
                )
        else:
            lines.append("  tc-private: (none)")

        if result.tc_public:
            for idx, tc in enumerate(result.tc_public):
                prefix = "  tc-public : " if idx == 0 else "              "
                lines.append(
                    prefix + _format_pr_line(tc, color=color).replace("\n  ", "\n              ")
                )
        else:
            lines.append("  tc-public : (none)")

    if result.cache is not None:
        lines.append("")
        lines.append(_format_cache_line(result.cache, color=color))

    return "\n".join(lines)


def _format_bundle_lines(
    idx: int,
    bundle: PrBundle,
    *,
    issue_key: str,
    color: bool,
) -> list[str]:
    lines: list[str] = []
    eng_txt = _format_pr_line(bundle.engine, color=color, current_issue_key=issue_key)
    eng_lines = eng_txt.splitlines()
    lines.append(f"  [{idx}] engine    : {eng_lines[0]}")
    for cont in eng_lines[1:]:
        lines.append(f"                  {cont.strip()}")

    if bundle.tc_private:
        for j, tc in enumerate(bundle.tc_private):
            tc_lines = _format_pr_line(tc, color=color, current_issue_key=issue_key).splitlines()
            prefix = "      tc-private: " if j == 0 else "                  "
            lines.append(f"{prefix}{tc_lines[0]}")
            for cont in tc_lines[1:]:
                lines.append(f"                  {cont.strip()}")
    else:
        lines.append("      tc-private: (none)")

    if bundle.tc_public:
        for j, tc in enumerate(bundle.tc_public):
            tc_lines = _format_pr_line(tc, color=color, current_issue_key=issue_key).splitlines()
            prefix = "      tc-public : " if j == 0 else "                  "
            lines.append(f"{prefix}{tc_lines[0]}")
            for cont in tc_lines[1:]:
                lines.append(f"                  {cont.strip()}")
    else:
        lines.append("      tc-public : (none)")
    return lines


def format_jira_human(
    result: JiraLookupResult,
    *,
    color: bool = False,
    heading_prefix: str = "",
) -> str:
    lines: list[str] = []
    iss = result.issue
    lines.append(heading_prefix + _format_jira_line(iss, color=color))

    lines.append("")
    lines.append(_c(f"PR Bundles ({len(result.bundles)}):", _ANSI_BOLD, color=color))
    if result.bundles:
        for idx, bundle in enumerate(result.bundles, start=1):
            lines.extend(_format_bundle_lines(idx, bundle, issue_key=iss.key, color=color))
    else:
        lines.append("  (none)")

    if result.standalone_tc_private or result.standalone_tc_public:
        lines.append("")
        lines.append(_c("Standalone TC PRs (no matched engine PR):", _ANSI_BOLD, color=color))
        for tc in result.standalone_tc_private:
            tc_lines = _format_pr_line(tc, color=color, current_issue_key=iss.key).splitlines()
            lines.append(f"  tc-private: {tc_lines[0]}")
            for cont in tc_lines[1:]:
                lines.append(f"              {cont.strip()}")
        for tc in result.standalone_tc_public:
            tc_lines = _format_pr_line(tc, color=color, current_issue_key=iss.key).splitlines()
            lines.append(f"  tc-public : {tc_lines[0]}")
            for cont in tc_lines[1:]:
                lines.append(f"              {cont.strip()}")

    if result.other_prs:
        lines.append("")
        lines.append(_c("Other Repository PRs:", _ANSI_BOLD, color=color))
        for pr in result.other_prs:
            pr_lines = _format_pr_line(pr, color=color, current_issue_key=iss.key).splitlines()
            lines.append(f"  {pr_lines[0]}")
            for cont in pr_lines[1:]:
                lines.append(f"    {cont.strip()}")

    if result.related_issues:
        lines.append("")
        lines.append(_c(f"Related JIRA Issues ({len(result.related_issues)}):", _ANSI_BOLD, color=color))
        for rel in result.related_issues:
            st = f" [{rel.status}]" if rel.status else ""
            lines.append(f"  - ({rel.relation}) {rel.key}{st}: {rel.summary} ({rel.url})")
        if not result.followups and not heading_prefix:
            lines.append(
                _c(
                    f"  Tip: run `cubrid-xref {iss.key} --follow-links` to resolve PRs for related issues.",
                    _ANSI_DIM,
                    color=color,
                )
            )

    if result.mentions:
        lines.append("")
        lines.append(_c(f"Mentioned In ({len(result.mentions)}):", _ANSI_BOLD, color=color))
        for pr in result.mentions:
            pr_lines = _format_pr_line(pr, color=color).splitlines()
            lines.append(f"  {pr_lines[0]}")
            for cont in pr_lines[1:]:
                lines.append(f"    {cont.strip()}")

    if result.followups:
        lines.append("")
        lines.append(_c(f"Follow-up / Related Issue PRs ({len(result.followups)}):", _ANSI_BOLD, color=color))
        for sub in result.followups:
            lines.append("")
            sub_text = format_jira_human(sub, color=color, heading_prefix="-> ")
            lines.extend("  " + ln if ln else "" for ln in sub_text.splitlines())

    if result.cache is not None and not heading_prefix:
        lines.append("")
        lines.append(_format_cache_line(result.cache, color=color))

    return "\n".join(lines)


def format_pr_plain(result: PrLookupResult) -> str:
    rows: list[str] = []
    pr = result.target_pr
    rows.append(
        "\t".join(["TARGET_PR", pr.role, pr.short_ref, pr.display_state, pr.url, pr.title])
    )
    for issue in result.primary_issues:
        rows.append(
            "\t".join(["JIRA_PRIMARY", issue.key, issue.status, issue.url, issue.summary])
        )
    for issue in result.mentioned_issues:
        rows.append(
            "\t".join(["JIRA_MENTION", issue.key, issue.status, issue.url, issue.summary])
        )
    if result.engine_pr is not None and (
        result.engine_pr.repo.lower() != pr.repo.lower() or result.engine_pr.number != pr.number
    ):
        ep = result.engine_pr
        rows.append("\t".join(["ENGINE_PR", ep.short_ref, ep.display_state, ep.url, ep.title]))
    for tc in result.tc_private:
        rows.append("\t".join(["TC_PRIVATE", tc.short_ref, tc.display_state, tc.url, tc.title]))
    for tc in result.tc_public:
        rows.append("\t".join(["TC_PUBLIC", tc.short_ref, tc.display_state, tc.url, tc.title]))
    return "\n".join(rows)


def _jira_plain_rows(result: JiraLookupResult, *, prefix: str = "") -> list[str]:
    rows: list[str] = []
    iss = result.issue
    rows.append(
        "\t".join([f"{prefix}JIRA", iss.key, iss.status, iss.url, iss.summary])
    )
    for idx, bundle in enumerate(result.bundles, start=1):
        eng = bundle.engine
        rows.append(
            "\t".join(
                [
                    f"{prefix}BUNDLE_{idx}_ENGINE",
                    eng.short_ref,
                    eng.display_state,
                    eng.url,
                    eng.title,
                ]
            )
        )
        for tc in bundle.tc_private:
            rows.append(
                "\t".join(
                    [
                        f"{prefix}BUNDLE_{idx}_TC_PRIVATE",
                        tc.short_ref,
                        tc.display_state,
                        tc.url,
                        tc.title,
                    ]
                )
            )
        for tc in bundle.tc_public:
            rows.append(
                "\t".join(
                    [
                        f"{prefix}BUNDLE_{idx}_TC_PUBLIC",
                        tc.short_ref,
                        tc.display_state,
                        tc.url,
                        tc.title,
                    ]
                )
            )
    for tc in result.standalone_tc_private:
        rows.append(
            "\t".join([f"{prefix}STANDALONE_TC_PRIVATE", tc.short_ref, tc.display_state, tc.url, tc.title])
        )
    for tc in result.standalone_tc_public:
        rows.append(
            "\t".join([f"{prefix}STANDALONE_TC_PUBLIC", tc.short_ref, tc.display_state, tc.url, tc.title])
        )
    for pr in result.other_prs:
        rows.append(
            "\t".join([f"{prefix}OTHER_PR", pr.short_ref, pr.display_state, pr.url, pr.title])
        )
    for rel in result.related_issues:
        rows.append(
            "\t".join([f"{prefix}RELATED_JIRA", rel.relation, rel.key, rel.status, rel.url, rel.summary])
        )
    for pr in result.mentions:
        rows.append(
            "\t".join([f"{prefix}MENTION_PR", pr.short_ref, pr.display_state, pr.url, pr.title])
        )
    for sub in result.followups:
        rows.extend(_jira_plain_rows(sub, prefix=f"{prefix}FOLLOWUP_{sub.issue.key}_"))
    return rows


def format_jira_plain(result: JiraLookupResult) -> str:
    return "\n".join(_jira_plain_rows(result))


def format_cache_stats_human(stats: dict[str, Any], *, color: bool = False) -> str:
    lines = [
        _c("cubrid-xref cache status", _ANSI_BOLD, color=color),
        f"Path   : {stats['path']}",
        f"Exists : {'yes' if stats['exists'] else 'no'}",
        f"Size   : {stats['size_bytes']} bytes",
        f"Entries: {stats['total_entries']} total ({stats['fresh_entries']} fresh, {stats['expired_entries']} expired)",
        f"By kind: pr={stats['by_kind'].get('pr', 0)}, jira={stats['by_kind'].get('jira', 0)}",
    ]
    if stats.get("oldest_fetched_at"):
        lines.append(f"Oldest : {stats['oldest_fetched_at']}")
    if stats.get("newest_fetched_at"):
        lines.append(f"Newest : {stats['newest_fetched_at']}")
    return "\n".join(lines)


def format_cache_stats_plain(stats: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"CACHE_PATH\t{stats['path']}",
            f"CACHE_EXISTS\t{str(stats['exists']).lower()}",
            f"CACHE_SIZE_BYTES\t{stats['size_bytes']}",
            f"CACHE_TOTAL\t{stats['total_entries']}",
            f"CACHE_FRESH\t{stats['fresh_entries']}",
            f"CACHE_EXPIRED\t{stats['expired_entries']}",
            f"CACHE_PR\t{stats['by_kind'].get('pr', 0)}",
            f"CACHE_JIRA\t{stats['by_kind'].get('jira', 0)}",
        ]
    )


def format_cache_clear_human(res: dict[str, Any], *, color: bool = False) -> str:
    return "\n".join(
        [
            _c("cubrid-xref cache clear", _ANSI_BOLD, color=color),
            f"Path     : {res['path']}",
            f"Removed  : {res['removed']}",
            f"Remaining: {res['remaining']}",
        ]
    )


def format_cache_clear_plain(res: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"CACHE_PATH\t{res['path']}",
            f"CACHE_REMOVED\t{res['removed']}",
            f"CACHE_REMAINING\t{res['remaining']}",
        ]
    )


def format_json_payload(data: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "data": data,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def format_json(result: PrLookupResult | JiraLookupResult) -> str:
    return format_json_payload(result.to_dict())


def format_error_human(err: CliError, *, color: bool = False) -> str:
    head = _c(f"Error: {err.code} — {err.message}", _ANSI_RED, color=color)
    lines = [head]
    if err.fix:
        lines.append(f"Fix: {err.fix}")
    return "\n".join(lines)


def format_error_json(err: CliError) -> str:
    return json.dumps(
        {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": err.to_dict(),
        },
        ensure_ascii=False,
    )
