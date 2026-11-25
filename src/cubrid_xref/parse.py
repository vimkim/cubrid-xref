"""Target parsing, repository classification, and PR/JIRA cross-reference extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from cubrid_xref.models import CliError

DEFAULT_ORG = "CUBRID"
DEFAULT_REPO = "CUBRID/cubrid"
ENGINE_REPO = "CUBRID/cubrid"
TC_PUBLIC_REPO = "CUBRID/cubrid-testcases"
TC_PRIVATE_EX_REPO = "CUBRID/cubrid-testcases-private-ex"
TC_PRIVATE_LEGACY_REPO = "CUBRID/cubrid-testcases-private"
DEFAULT_JIRA_SERVER = "https://jira.cubrid.org"

KNOWN_JIRA_PREFIXES = frozenset(
    {
        "CBRD",
        "CUBRIDSUS",
        "LETS",
        "APIS",
        "TOOLS",
        "QA",
        "DOC",
        "CM",
        "CCI",
        "JDBC",
        "ODBC",
        "PHP",
        "PYTHON",
        "NODE",
        "CUBRID",
    }
)

FALSE_POSITIVE_PREFIXES = frozenset(
    {
        "PR",
        "UTF",
        "SHA",
        "MD",
        "HTTP",
        "HTTPS",
        "X86",
        "ARM",
        "ISO",
        "RFC",
        "CVE",
        "TLS",
        "SSL",
        "AES",
        "RSA",
        "ECDSA",
        "GCC",
        "CLANG",
        "LLVM",
        "GLIBC",
        "POSIX",
        "IEEE",
        "ANSI",
        "ASCII",
        "SQL",
        "JDBC",  # Wait: JDBC is in KNOWN_JIRA_PREFIXES! Do NOT put JDBC here.
    }
)
# Ensure known prefixes always win over false-positive exclusions.
FALSE_POSITIVE_PREFIXES = FALSE_POSITIVE_PREFIXES - KNOWN_JIRA_PREFIXES | frozenset({"PR"})

_GITHUB_PR_URL_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_JIRA_BROWSE_URL_RE = re.compile(
    r"^(https?://jira\.cubrid\.(?:org|com))/browse/([A-Za-z][A-Za-z0-9]*-\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_REPO_PR_REF_RE = re.compile(
    r"^(?:([A-Za-z0-9_.-]+)/)?([A-Za-z0-9_.-]+)#(\d+)$"
)
_BARE_PR_PREFIX_RE = re.compile(r"^(?:#|pr[-_]?|PR[-_]?)(\d+)$")
_BARE_DIGITS_RE = re.compile(r"^(\d+)$")
_JIRA_KEY_EXACT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-(\d+)$")

_TITLE_BRACKET_KEY_RE = re.compile(r"\[([^\]]+)\]")
_ANY_KEY_UPPER_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,15}-\d+)\b")
_CBRD_CASE_INSENSITIVE_RE = re.compile(r"\b(cbrd-\d+)\b", re.IGNORECASE)
_BODY_JIRA_BROWSE_RE = re.compile(
    r"https?://jira\.cubrid\.(?:org|com)/browse/([A-Za-z][A-Za-z0-9]*-\d+)\b",
    re.IGNORECASE,
)
_HEAD_CBRD_RE = re.compile(
    r"(?:^|[/._-])(CBRD)[-_]?(\d+)(?:$|[^0-9])",
    re.IGNORECASE,
)
_HEAD_KNOWN_RE = re.compile(
    r"(?:^|[/._-])(" + "|".join(sorted(KNOWN_JIRA_PREFIXES)) + r")[-_](\d+)(?:$|[^0-9])",
    re.IGNORECASE,
)

_TC_HEAD_PR_RE = re.compile(r"(?:^|[-/])tc/pr-(\d+)\b", re.IGNORECASE)
_TC_TITLE_ENGINE_RE = re.compile(r"\bCUBRID/cubrid#(\d+)\b", re.IGNORECASE)
_TC_BODY_ENGINE_RE = re.compile(
    r"(?:Related\s+)?Engine\s+PR\s*:\s*(?:CUBRID/cubrid#|https?://github\.com/CUBRID/cubrid/pull/)(\d+)\b",
    re.IGNORECASE,
)
_REVERT_PR_RE = re.compile(r'^Revert\s+".*\(#(\d+)\)"')
_GITHUB_PR_EMBED_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedTarget:
    kind: Literal["pr", "jira"]
    repo: str | None = None
    number: int | None = None
    issue_key: str | None = None
    jira_server: str | None = None


def classify_repo(repo: str) -> str:
    """Classify a GitHub repository into engine, tc_public, tc_private, or other."""
    low = repo.strip().lower()
    if low == ENGINE_REPO.lower():
        return "engine"
    if low == TC_PUBLIC_REPO.lower():
        return "tc_public"
    if low in {TC_PRIVATE_EX_REPO.lower(), TC_PRIVATE_LEGACY_REPO.lower()}:
        return "tc_private"
    return "other"


def _is_valid_jira_key(key: str, *, allow_unknown_prefix: bool = False) -> bool:
    m = _JIRA_KEY_EXACT_RE.match(key.strip())
    if not m:
        return False
    prefix = m.group(1).upper()
    if prefix in FALSE_POSITIVE_PREFIXES:
        return False
    if prefix in KNOWN_JIRA_PREFIXES:
        return True
    return allow_unknown_prefix and len(prefix) >= 2


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_target(
    raw: str,
    *,
    default_repo: str = DEFAULT_REPO,
    forced_kind: Literal["pr", "jira"] | None = None,
) -> ParsedTarget:
    """Parse a CLI target string into a PR or JIRA lookup target."""
    text = raw.strip()
    if not text:
        raise CliError(
            "INVALID_TARGET",
            "Target argument cannot be empty.",
            "Pass a PR URL/number (e.g. 8693) or JIRA issue key (e.g. CBRD-28257).",
            exit_code=2,
        )

    m_gh = _GITHUB_PR_URL_RE.match(text)
    if m_gh:
        owner, repo_name, num_str = m_gh.groups()
        target = ParsedTarget(kind="pr", repo=f"{owner}/{repo_name}", number=int(num_str))
        if forced_kind == "jira":
            raise CliError(
                "INVALID_TARGET",
                f"Expected a JIRA issue key or URL, got GitHub PR URL: {raw}",
                "Use `cubrid-xref pr <target>` or omit the `jira` subcommand.",
                exit_code=2,
            )
        return target

    m_jira_url = _JIRA_BROWSE_URL_RE.match(text)
    if m_jira_url:
        server, key = m_jira_url.groups()
        target = ParsedTarget(
            kind="jira",
            issue_key=key.upper(),
            jira_server=server.rstrip("/"),
        )
        if forced_kind == "pr":
            raise CliError(
                "INVALID_TARGET",
                f"Expected a GitHub PR reference, got JIRA URL: {raw}",
                "Use `cubrid-xref jira <target>` or omit the `pr` subcommand.",
                exit_code=2,
            )
        return target

    m_repo_ref = _REPO_PR_REF_RE.match(text)
    if m_repo_ref:
        owner, repo_name, num_str = m_repo_ref.groups()
        repo = f"{owner or DEFAULT_ORG}/{repo_name}"
        if forced_kind == "jira":
            raise CliError(
                "INVALID_TARGET",
                f"Expected a JIRA issue key or URL, got PR reference: {raw}",
                "Use `cubrid-xref pr <target>` or omit the `jira` subcommand.",
                exit_code=2,
            )
        return ParsedTarget(kind="pr", repo=repo, number=int(num_str))

    m_bare_pr = _BARE_PR_PREFIX_RE.match(text)
    if m_bare_pr:
        if forced_kind == "jira":
            raise CliError(
                "INVALID_TARGET",
                f"Expected a JIRA issue key or URL, got PR reference: {raw}",
                "Pass an issue key like CBRD-28257 or digits like 28257.",
                exit_code=2,
            )
        return ParsedTarget(kind="pr", repo=default_repo, number=int(m_bare_pr.group(1)))

    m_digits = _BARE_DIGITS_RE.match(text)
    if m_digits:
        num = int(m_digits.group(1))
        if forced_kind == "jira":
            return ParsedTarget(kind="jira", issue_key=f"CBRD-{num}")
        return ParsedTarget(kind="pr", repo=default_repo, number=num)

    m_key = _JIRA_KEY_EXACT_RE.match(text)
    if m_key:
        prefix = m_key.group(1).upper()
        key = f"{prefix}-{m_key.group(2)}"
        if prefix == "PR":
            if forced_kind == "jira":
                raise CliError(
                    "INVALID_TARGET",
                    f"Expected a JIRA issue key or URL, got PR reference: {raw}",
                    "Pass an issue key like CBRD-28257.",
                    exit_code=2,
                )
            return ParsedTarget(kind="pr", repo=default_repo, number=int(m_key.group(2)))
        if _is_valid_jira_key(key, allow_unknown_prefix=(forced_kind == "jira")):
            if forced_kind == "pr":
                raise CliError(
                    "INVALID_TARGET",
                    f"Expected a GitHub PR reference, got JIRA key: {raw}",
                    "Use `cubrid-xref jira <target>` or omit the `pr` subcommand.",
                    exit_code=2,
                )
            return ParsedTarget(kind="jira", issue_key=key)

    raise CliError(
        "INVALID_TARGET",
        f"Unrecognized PR or JIRA target: {raw}",
        "Examples: 8693, CUBRID/cubrid#8693, https://github.com/CUBRID/cubrid/pull/8693, CBRD-28257",
        exit_code=2,
    )


def _extract_keys_from_text(
    text: str,
    *,
    case_insensitive_cbrd: bool = False,
    allow_bracket_unknown: bool = False,
) -> list[str]:
    found: list[str] = []
    if allow_bracket_unknown:
        for bracket in _TITLE_BRACKET_KEY_RE.findall(text):
            for token in re.findall(r"[A-Za-z][A-Za-z0-9]*-\d+", bracket):
                up = token.upper()
                if _is_valid_jira_key(up, allow_unknown_prefix=True):
                    found.append(up)
    if case_insensitive_cbrd:
        for m in _CBRD_CASE_INSENSITIVE_RE.findall(text):
            found.append(m.upper())
    for m in _ANY_KEY_UPPER_RE.findall(text):
        if _is_valid_jira_key(m, allow_unknown_prefix=False):
            found.append(m)
    return _dedupe_preserve(found)


def extract_jira_keys_from_pr(
    title: str,
    body: str,
    head_ref: str = "",
) -> tuple[list[str], list[str]]:
    """Extract (primary_keys, mentioned_keys) from a PR's title, body, and branch."""
    primary: list[str] = []

    # 1. Title keys (supports [CBRD-1][CBRD-2], [CBRD-1, CBRD-2], [APIS-123], etc.)
    primary.extend(
        _extract_keys_from_text(
            title or "",
            case_insensitive_cbrd=True,
            allow_bracket_unknown=True,
        )
    )

    # 2. Top 5 non-empty lines of body for explicit JIRA browse links
    body_text = body or ""
    top_lines = [line.strip() for line in body_text.splitlines() if line.strip()][:5]
    for line in top_lines:
        for key in _BODY_JIRA_BROWSE_RE.findall(line):
            up = key.upper()
            if _is_valid_jira_key(up, allow_unknown_prefix=True):
                primary.append(up)

    # 3. Fallback to head_ref only when title and top-of-body yielded nothing
    if not primary and head_ref:
        for prefix, num in _HEAD_CBRD_RE.findall(head_ref):
            primary.append(f"{prefix.upper()}-{num}")
        if not primary:
            for prefix, num in _HEAD_KNOWN_RE.findall(head_ref):
                primary.append(f"{prefix.upper()}-{num}")

    primary = _dedupe_preserve(primary)
    primary_set = set(primary)

    # 4. Mentioned keys from body (browse URLs + uppercase known keys; ignore lowercase cbrd_27780)
    mentioned: list[str] = []
    for key in _BODY_JIRA_BROWSE_RE.findall(body_text):
        up = key.upper()
        if up not in primary_set and _is_valid_jira_key(up, allow_unknown_prefix=True):
            mentioned.append(up)
    for key in _extract_keys_from_text(
        body_text,
        case_insensitive_cbrd=False,
        allow_bracket_unknown=False,
    ):
        if key not in primary_set:
            mentioned.append(key)

    return primary, _dedupe_preserve(mentioned)


def extract_engine_pr_refs_from_tc_pr(
    title: str,
    body: str,
    head_ref: str = "",
) -> list[int]:
    """Extract linked CUBRID/cubrid engine PR numbers from a TC PR."""
    refs: list[int] = []
    if head_ref:
        for num in _TC_HEAD_PR_RE.findall(head_ref):
            refs.append(int(num))
    if title:
        for num in _TC_TITLE_ENGINE_RE.findall(title):
            refs.append(int(num))
    if body:
        for num in _TC_BODY_ENGINE_RE.findall(body):
            refs.append(int(num))

    seen: set[int] = set()
    out: list[int] = []
    for num in refs:
        if num not in seen:
            seen.add(num)
            out.append(num)
    return out


def extract_reverted_pr_number(title: str) -> int | None:
    """Return the reverted PR number from a GitHub `Revert \"... (#N)\"` title."""
    m = _REVERT_PR_RE.match((title or "").strip())
    return int(m.group(1)) if m else None


def extract_github_pr_urls(text: str) -> list[tuple[str, int]]:
    """Extract (owner/repo, number) pairs from GitHub PR URLs in arbitrary text."""
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for owner, repo, num_str in _GITHUB_PR_EMBED_RE.findall(text or ""):
        item = (f"{owner}/{repo}", int(num_str))
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
