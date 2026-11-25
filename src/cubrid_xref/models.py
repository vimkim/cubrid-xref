"""Data models and domain errors for cubrid-xref."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class CliError(Exception):
    """Structured error mapped to exit codes and stderr output by the CLI."""

    def __init__(
        self,
        code: str,
        message: str,
        fix: str = "",
        *,
        exit_code: int = 1,
        transient: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.exit_code = exit_code
        self.transient = transient
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "fix": self.fix,
            "transient": self.transient,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class CacheMeta:
    hit: bool
    fetched_at: str
    age_seconds: float
    ttl_seconds: float
    stale: bool = False
    cache_path: str = ""
    source: str = "live"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CacheMeta:
        return cls(
            hit=bool(data.get("hit", False)),
            fetched_at=str(data.get("fetched_at", "")),
            age_seconds=float(data.get("age_seconds", 0.0)),
            ttl_seconds=float(data.get("ttl_seconds", 86400.0)),
            stale=bool(data.get("stale", False)),
            cache_path=str(data.get("cache_path", "")),
            source=str(data.get("source", "live")),
        )


@dataclass(frozen=True)
class JiraIssue:
    key: str
    url: str
    summary: str = ""
    status: str = ""
    issue_type: str = ""
    resolution: str = ""
    assignee: str = ""
    fix_versions: tuple[str, ...] = ()
    found: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fix_versions"] = list(self.fix_versions)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JiraIssue:
        return cls(
            key=str(data["key"]),
            url=str(data.get("url", "")),
            summary=str(data.get("summary", "")),
            status=str(data.get("status", "")),
            issue_type=str(data.get("issue_type", "")),
            resolution=str(data.get("resolution", "")),
            assignee=str(data.get("assignee", "")),
            fix_versions=tuple(str(v) for v in (data.get("fix_versions") or ())),
            found=bool(data.get("found", True)),
        )


@dataclass(frozen=True)
class RelatedJiraIssue:
    relation: str
    key: str
    url: str
    summary: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RelatedJiraIssue:
        return cls(
            relation=str(data.get("relation", "")),
            key=str(data["key"]),
            url=str(data.get("url", "")),
            summary=str(data.get("summary", "")),
            status=str(data.get("status", "")),
        )


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    state: str
    is_draft: bool = False
    merged_at: str | None = None
    base_ref: str = ""
    head_ref: str = ""
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    role: str = "other"
    primary_issues: tuple[str, ...] = ()
    mentioned_issues: tuple[str, ...] = ()
    body: str = ""

    @property
    def display_state(self) -> str:
        state_low = self.state.lower()
        if self.merged_at or state_low == "merged":
            return "MERGED"
        if state_low == "closed" and self.is_draft:
            return "CLOSED/DRAFT"
        if state_low == "closed":
            return "CLOSED"
        if self.is_draft:
            return "DRAFT"
        return "OPEN"

    @property
    def short_ref(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def repo_short(self) -> str:
        return self.repo.split("/", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("body", None)
        data["display_state"] = self.display_state
        data["short_ref"] = self.short_ref
        data["primary_issues"] = list(self.primary_issues)
        data["mentioned_issues"] = list(self.mentioned_issues)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PullRequest:
        return cls(
            repo=str(data["repo"]),
            number=int(data["number"]),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            state=str(data.get("state", "open")),
            is_draft=bool(data.get("is_draft", False)),
            merged_at=data.get("merged_at"),
            base_ref=str(data.get("base_ref", "")),
            head_ref=str(data.get("head_ref", "")),
            author=str(data.get("author", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            role=str(data.get("role", "other")),
            primary_issues=tuple(str(k) for k in (data.get("primary_issues") or ())),
            mentioned_issues=tuple(str(k) for k in (data.get("mentioned_issues") or ())),
        )


@dataclass(frozen=True)
class PrBundle:
    engine: PullRequest
    tc_private: tuple[PullRequest, ...] = ()
    tc_public: tuple[PullRequest, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine.to_dict(),
            "tc_private": [pr.to_dict() for pr in self.tc_private],
            "tc_public": [pr.to_dict() for pr in self.tc_public],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrBundle:
        return cls(
            engine=PullRequest.from_dict(data["engine"]),
            tc_private=tuple(PullRequest.from_dict(p) for p in (data.get("tc_private") or ())),
            tc_public=tuple(PullRequest.from_dict(p) for p in (data.get("tc_public") or ())),
        )


@dataclass(frozen=True)
class PrLookupResult:
    target_pr: PullRequest
    primary_issues: tuple[JiraIssue, ...] = ()
    mentioned_issues: tuple[JiraIssue, ...] = ()
    engine_pr: PullRequest | None = None
    tc_private: tuple[PullRequest, ...] = ()
    tc_public: tuple[PullRequest, ...] = ()
    warnings: tuple[str, ...] = ()
    cache: CacheMeta | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "pr",
            "target_pr": self.target_pr.to_dict(),
            "primary_issues": [i.to_dict() for i in self.primary_issues],
            "mentioned_issues": [i.to_dict() for i in self.mentioned_issues],
            "engine_pr": self.engine_pr.to_dict() if self.engine_pr else None,
            "tc_private": [pr.to_dict() for pr in self.tc_private],
            "tc_public": [pr.to_dict() for pr in self.tc_public],
            "warnings": list(self.warnings),
        }
        if self.cache is not None:
            payload["cache"] = self.cache.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, cache: CacheMeta | None = None) -> PrLookupResult:
        raw_cache = data.get("cache")
        resolved_cache = cache if cache is not None else (
            CacheMeta.from_dict(raw_cache) if isinstance(raw_cache, dict) else None
        )
        return cls(
            target_pr=PullRequest.from_dict(data["target_pr"]),
            primary_issues=tuple(JiraIssue.from_dict(i) for i in (data.get("primary_issues") or ())),
            mentioned_issues=tuple(
                JiraIssue.from_dict(i) for i in (data.get("mentioned_issues") or ())
            ),
            engine_pr=(
                PullRequest.from_dict(data["engine_pr"])
                if data.get("engine_pr") is not None
                else None
            ),
            tc_private=tuple(PullRequest.from_dict(p) for p in (data.get("tc_private") or ())),
            tc_public=tuple(PullRequest.from_dict(p) for p in (data.get("tc_public") or ())),
            warnings=tuple(str(w) for w in (data.get("warnings") or ())),
            cache=resolved_cache,
        )


@dataclass(frozen=True)
class JiraLookupResult:
    issue: JiraIssue
    bundles: tuple[PrBundle, ...] = ()
    standalone_tc_private: tuple[PullRequest, ...] = ()
    standalone_tc_public: tuple[PullRequest, ...] = ()
    other_prs: tuple[PullRequest, ...] = ()
    related_issues: tuple[RelatedJiraIssue, ...] = ()
    mentions: tuple[PullRequest, ...] = ()
    followups: tuple[JiraLookupResult, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = ()
    cache: CacheMeta | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "jira",
            "issue": self.issue.to_dict(),
            "bundles": [b.to_dict() for b in self.bundles],
            "standalone_tc_private": [pr.to_dict() for pr in self.standalone_tc_private],
            "standalone_tc_public": [pr.to_dict() for pr in self.standalone_tc_public],
            "other_prs": [pr.to_dict() for pr in self.other_prs],
            "related_issues": [r.to_dict() for r in self.related_issues],
            "mentions": [pr.to_dict() for pr in self.mentions],
            "followups": [f.to_dict() for f in self.followups],
            "warnings": list(self.warnings),
        }
        if self.cache is not None:
            payload["cache"] = self.cache.to_dict()
        return payload

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        cache: CacheMeta | None = None,
    ) -> JiraLookupResult:
        raw_cache = data.get("cache")
        resolved_cache = cache if cache is not None else (
            CacheMeta.from_dict(raw_cache) if isinstance(raw_cache, dict) else None
        )
        return cls(
            issue=JiraIssue.from_dict(data["issue"]),
            bundles=tuple(PrBundle.from_dict(b) for b in (data.get("bundles") or ())),
            standalone_tc_private=tuple(
                PullRequest.from_dict(p) for p in (data.get("standalone_tc_private") or ())
            ),
            standalone_tc_public=tuple(
                PullRequest.from_dict(p) for p in (data.get("standalone_tc_public") or ())
            ),
            other_prs=tuple(PullRequest.from_dict(p) for p in (data.get("other_prs") or ())),
            related_issues=tuple(
                RelatedJiraIssue.from_dict(r) for r in (data.get("related_issues") or ())
            ),
            mentions=tuple(PullRequest.from_dict(p) for p in (data.get("mentions") or ())),
            followups=tuple(
                cls.from_dict(f) for f in (data.get("followups") or ())
            ),
            warnings=tuple(str(w) for w in (data.get("warnings") or ())),
            cache=resolved_cache,
        )
