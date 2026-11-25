"""CLI entry point for cubrid-xref."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Sequence

from cubrid_xref import __version__
from cubrid_xref.cache import XrefCache, default_cache_path, default_ttl_hours
from cubrid_xref.clients import GitHubClient, JiraClient
from cubrid_xref.formatters import (
    OutputMode,
    format_cache_clear_human,
    format_cache_clear_plain,
    format_cache_stats_human,
    format_cache_stats_plain,
    format_error_human,
    format_error_json,
    format_jira_human,
    format_jira_plain,
    format_json,
    format_json_payload,
    format_pr_human,
    format_pr_plain,
    resolve_output_mode,
)
from cubrid_xref.models import CliError, JiraLookupResult, PrLookupResult
from cubrid_xref.parse import DEFAULT_JIRA_SERVER, DEFAULT_REPO, parse_target
from cubrid_xref.resolver import resolve_jira, resolve_pr

_VALUE_FLAGS = frozenset(
    {"--repo", "--jira-server", "--max-related", "--timeout", "--ttl", "--cache-dir"}
)
_SUBCOMMANDS = frozenset({"pr", "jira", "cache"})


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - wrapped in CliError
        raise CliError(
            "INVALID_USAGE",
            message,
            "Run `cubrid-xref --help` for usage and examples.",
            exit_code=2,
        )


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="cubrid-xref",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Bidirectional lookup and 24h caching helper between CUBRID GitHub PRs "
            "(engine + tc-private + tc-public) and JIRA issues."
        ),
        epilog="""Examples:
  cubrid-xref 8693
  cubrid-xref https://github.com/CUBRID/cubrid/pull/8693
  cubrid-xref cubrid-testcases-private-ex#5098
  cubrid-xref CBRD-28257
  cubrid-xref jira CBRD-26357 --follow-links
  cubrid-xref --refresh CBRD-28257
  cubrid-xref --cache-only --json CBRD-28257
  cubrid-xref cache status
  cubrid-xref cache clear --expired

Exit codes:
  0   Success
  1   Target not found, cache miss (--cache-only), or domain failure
  2   Invalid usage or unrecognized target
  75  Temporary upstream/rate-limit failure (retry-safe)
  78  Missing or invalid authentication/configuration
""",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="PR URL/number or JIRA key/URL (or `pr` / `jira` / `cache` mode).",
    )
    parser.add_argument(
        "action",
        nargs="?",
        help="Optional `status` or `clear` action for `cubrid-xref cache`.",
    )
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    parser.add_argument("--plain", action="store_true", help="Output grep-friendly TSV rows.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    parser.add_argument(
        "-r",
        "--refresh",
        "-u",
        "--update",
        action="store_true",
        dest="refresh",
        help="Force live fetch and update local cache.",
    )
    parser.add_argument(
        "--cache-only",
        "--offline",
        action="store_true",
        dest="cache_only",
        help="Read from local cache without network requests.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass local cache read and write.",
    )
    parser.add_argument(
        "--ttl",
        type=float,
        default=None,
        help="Cache TTL in hours (default: $CUBRID_XREF_CACHE_TTL_HOURS or 24).",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Override cache directory (default: $CUBRID_XREF_CACHE_DIR or ~/.cache/cubrid-xref).",
    )
    parser.add_argument(
        "--expired",
        action="store_true",
        help="With `cache clear`, delete only expired entries.",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Default GitHub repository for bare PR numbers (default: {DEFAULT_REPO}).",
    )
    parser.add_argument(
        "--jira-server",
        default=None,
        help=f"JIRA server base URL (default: $CUBRID_JIRA_SERVER or {DEFAULT_JIRA_SERVER}).",
    )
    parser.add_argument("--no-jira", action="store_true", help="Skip JIRA REST queries.")
    parser.add_argument(
        "--include-mentions",
        action="store_true",
        help="Include PRs that only mention the JIRA key in body/comments.",
    )
    parser.add_argument(
        "--follow-links",
        action="store_true",
        help="Also resolve PRs for linked/subtask JIRA issues (follow-ups).",
    )
    parser.add_argument(
        "--max-related",
        type=int,
        default=10,
        help="Max related JIRA issues resolved with --follow-links (default: 10).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds (default: 15).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _has_positional(tokens: Sequence[str]) -> bool:
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if any(tok.startswith(f + "=") for f in _VALUE_FLAGS):
            continue
        if tok in _VALUE_FLAGS:
            skip_next = True
            continue
        if tok == "--" or tok.startswith("-"):
            continue
        return True
    return False


def _extract_subcommand(argv: Sequence[str]) -> tuple[str | None, list[str]]:
    """Extract an optional `pr`, `jira`, or `cache` mode token regardless of flag order."""
    out: list[str] = []
    subcommand: str | None = None
    skip_next = False
    seen_double_dash = False

    for token in argv:
        if skip_next:
            out.append(token)
            skip_next = False
            continue
        if seen_double_dash:
            if subcommand is None and token in _SUBCOMMANDS and not _has_positional(out):
                subcommand = token
                continue
            out.append(token)
            continue
        if token == "--":
            seen_double_dash = True
            out.append(token)
            continue
        if any(token.startswith(f + "=") for f in _VALUE_FLAGS):
            out.append(token)
            continue
        if token in _VALUE_FLAGS:
            out.append(token)
            skip_next = True
            continue
        if token.startswith("-"):
            out.append(token)
            continue
        if subcommand is None and token in _SUBCOMMANDS and not _has_positional(out):
            subcommand = token
            continue
        out.append(token)

    return subcommand, out


def _handle_cache_subcommand(
    args: argparse.Namespace,
    mode: OutputMode,
    cache: XrefCache,
    ttl_seconds: float,
) -> int:
    action = args.target
    if args.action is not None:
        raise CliError(
            "INVALID_USAGE",
            f"Unexpected argument for `cache {action}`: {args.action}",
            "Use `cubrid-xref cache status` or `cubrid-xref cache clear [--expired]`.",
            exit_code=2,
        )
    if action == "status":
        if args.expired:
            raise CliError(
                "INVALID_USAGE",
                "`--expired` is only valid with `cubrid-xref cache clear`.",
                "Run `cubrid-xref cache clear --expired`.",
                exit_code=2,
            )
        stats = cache.stats(ttl_seconds=ttl_seconds)
        if mode.fmt == "json":
            print(format_json_payload({"kind": "cache_status", **stats}))
        elif mode.fmt == "plain":
            print(format_cache_stats_plain(stats))
        else:
            print(format_cache_stats_human(stats, color=mode.color))
        return 0
    if action == "clear":
        res = cache.clear(expired_only=args.expired, ttl_seconds=ttl_seconds)
        if mode.fmt == "json":
            print(format_json_payload({"kind": "cache_clear", **res}))
        elif mode.fmt == "plain":
            print(format_cache_clear_plain(res))
        else:
            print(format_cache_clear_human(res, color=mode.color))
        return 0
    raise CliError(
        "INVALID_USAGE",
        f"Invalid or missing cache action: {action or '(none)'}",
        "Use `cubrid-xref cache status` or `cubrid-xref cache clear [--expired]`.",
        exit_code=2,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    gh_factory: Callable[..., GitHubClient] | None = None,
    jira_factory: Callable[..., JiraClient] | None = None,
    cache_factory: Callable[[Path], XrefCache] | None = None,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    json_hint = "--json" in raw_argv
    plain_hint = "--plain" in raw_argv
    no_color_hint = "--no-color" in raw_argv

    mode: OutputMode = resolve_output_mode(
        json_flag=json_hint,
        plain_flag=plain_hint,
        no_color_flag=no_color_hint,
        stdout_is_tty=getattr(sys.stdout, "isatty", lambda: False)(),
        env=os.environ,
    )

    try:
        subcommand, normalized_argv = _extract_subcommand(raw_argv)
        parser = _build_parser()
        args, extra = parser.parse_known_args(normalized_argv)
        if extra:
            raise CliError(
                "INVALID_USAGE",
                f"Unexpected extra argument(s): {' '.join(extra)}",
                "Pass a single target. Run `cubrid-xref --help` for examples.",
                exit_code=2,
            )

        if args.json and args.plain:
            raise CliError(
                "INVALID_USAGE",
                "Cannot combine --json and --plain.",
                "Choose either `--json` or `--plain`.",
                exit_code=2,
            )

        if args.timeout <= 0:
            raise CliError(
                "INVALID_USAGE",
                "--timeout must be greater than 0.",
                "Pass a positive number of seconds, e.g. `--timeout 15`.",
                exit_code=2,
            )
        if args.max_related < 0:
            raise CliError(
                "INVALID_USAGE",
                "--max-related cannot be negative.",
                "Pass 0 or a positive integer.",
                exit_code=2,
            )

        ttl_hours = default_ttl_hours() if args.ttl is None else float(args.ttl)
        if ttl_hours < 0:
            raise CliError(
                "INVALID_USAGE",
                "--ttl cannot be negative.",
                "Pass 0 or a positive number of hours, e.g. `--ttl 24`.",
                exit_code=2,
            )
        ttl_seconds = ttl_hours * 3600.0

        cache_path = default_cache_path(args.cache_dir)
        cache_obj = (cache_factory or XrefCache)(cache_path)

        if subcommand == "cache":
            return _handle_cache_subcommand(args, mode, cache_obj, ttl_seconds)

        if args.action is not None:
            raise CliError(
                "INVALID_USAGE",
                f"Unexpected extra argument: {args.action}",
                "Pass a single target. Run `cubrid-xref --help` for examples.",
                exit_code=2,
            )

        if args.expired:
            raise CliError(
                "INVALID_USAGE",
                "`--expired` is only valid with `cubrid-xref cache clear`.",
                "Run `cubrid-xref cache clear --expired`.",
                exit_code=2,
            )

        if args.refresh and args.cache_only:
            raise CliError(
                "INVALID_USAGE",
                "Cannot combine --refresh and --cache-only.",
                "Use `--refresh` to fetch live updates or `--cache-only` to read offline.",
                exit_code=2,
            )
        if args.refresh and args.no_cache:
            raise CliError(
                "INVALID_USAGE",
                "Cannot combine --refresh and --no-cache.",
                "Use `--refresh` to update the cache, or `--no-cache` to bypass it.",
                exit_code=2,
            )
        if args.cache_only and args.no_cache:
            raise CliError(
                "INVALID_USAGE",
                "Cannot combine --cache-only and --no-cache.",
                "Remove `--no-cache` when reading offline with `--cache-only`.",
                exit_code=2,
            )

        if not args.target:
            raise CliError(
                "INVALID_USAGE",
                "Missing target PR or JIRA issue.",
                "Examples: `cubrid-xref 8693` or `cubrid-xref CBRD-28257`. Use `--help` for more.",
                exit_code=2,
            )

        active_cache: XrefCache | None = None if args.no_cache else cache_obj

        target = parse_target(
            args.target,
            default_repo=args.repo,
            forced_kind=subcommand,  # type: ignore[arg-type]
        )

        jira_server = (
            args.jira_server
            or target.jira_server
            or os.environ.get("CUBRID_JIRA_SERVER")
            or DEFAULT_JIRA_SERVER
        ).rstrip("/")

        gh = (gh_factory or GitHubClient)(timeout=args.timeout)
        jira = (jira_factory or JiraClient)(server=jira_server, timeout=args.timeout)

        if target.kind == "pr":
            assert target.repo is not None and target.number is not None
            pr_result: PrLookupResult = resolve_pr(
                target.repo,
                target.number,
                gh,
                jira,
                fetch_jira=not args.no_jira,
                jira_server=jira_server,
                cache=active_cache,
                refresh=args.refresh,
                cache_only=args.cache_only,
                ttl_seconds=ttl_seconds,
            )
            for warn in pr_result.warnings:
                print(f"Warning: {warn}", file=sys.stderr)
            if pr_result.cache and pr_result.cache.stale:
                print(
                    f"Warning: returning stale cached entry (fetched {pr_result.cache.fetched_at}); "
                    "run with --refresh when online.",
                    file=sys.stderr,
                )
            if mode.fmt == "json":
                print(format_json(pr_result))
            elif mode.fmt == "plain":
                print(format_pr_plain(pr_result))
            else:
                print(format_pr_human(pr_result, color=mode.color))
            return 0

        assert target.issue_key is not None
        jira_result: JiraLookupResult = resolve_jira(
            target.issue_key,
            gh,
            jira,
            fetch_jira=not args.no_jira,
            include_mentions=args.include_mentions,
            follow_links=args.follow_links,
            max_related=args.max_related,
            jira_server=jira_server,
            cache=active_cache,
            refresh=args.refresh,
            cache_only=args.cache_only,
            ttl_seconds=ttl_seconds,
        )
        for warn in jira_result.warnings:
            print(f"Warning: {warn}", file=sys.stderr)
        if jira_result.cache and jira_result.cache.stale:
            print(
                f"Warning: returning stale cached entry (fetched {jira_result.cache.fetched_at}); "
                "run with --refresh when online.",
                file=sys.stderr,
            )
        if mode.fmt == "json":
            print(format_json(jira_result))
        elif mode.fmt == "plain":
            print(format_jira_plain(jira_result))
        else:
            print(format_jira_human(jira_result, color=mode.color))
        return 0

    except CliError as err:
        if mode.fmt == "json":
            print(format_error_json(err), file=sys.stderr)
        else:
            stderr_tty = getattr(sys.stderr, "isatty", lambda: False)()
            err_color = mode.color and stderr_tty
            print(format_error_human(err, color=err_color), file=sys.stderr)
        return err.exit_code
    except KeyboardInterrupt:
        return 130
