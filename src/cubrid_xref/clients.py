"""HTTP clients for GitHub REST API and CUBRID JIRA REST API."""

from __future__ import annotations

import base64
import json
import netrc
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cubrid_xref.models import CliError, JiraIssue, PullRequest, RelatedJiraIssue
from cubrid_xref.parse import (
    DEFAULT_JIRA_SERVER,
    classify_repo,
    extract_github_pr_urls,
    extract_jira_keys_from_pr,
)

_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _parse_repo_from_api_url(repo_url: str) -> str:
    parts = [p for p in urllib.parse.urlparse(repo_url).path.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return repo_url


class GitHubClient:
    """Thin GitHub REST API v3 client using stdlib urllib and gh/env token auth."""

    def __init__(self, *, timeout: float = 15.0, token: str | None = None) -> None:
        self.timeout = timeout
        self._token = token
        self._lock = threading.Lock()

    def _resolve_token(self) -> str:
        with self._lock:
            if self._token:
                return self._token
            for env_name in ("GH_TOKEN", "GITHUB_TOKEN"):
                val = os.environ.get(env_name, "").strip()
                if val:
                    self._token = val
                    return val
            try:
                proc = subprocess.run(
                    ["gh", "auth", "token"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise CliError(
                    "AUTH_MISSING",
                    "GitHub CLI `gh` not found and GH_TOKEN/GITHUB_TOKEN is unset.",
                    "Export GH_TOKEN or run `gh auth login`.",
                    exit_code=78,
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise CliError(
                    "UPSTREAM_UNAVAILABLE",
                    "Timed out running `gh auth token`.",
                    "Set GH_TOKEN or retry.",
                    exit_code=75,
                    transient=True,
                ) from exc

            tok = proc.stdout.strip()
            if proc.returncode == 0 and tok:
                self._token = tok
                return tok

            raise CliError(
                "AUTH_MISSING",
                "No GitHub authentication token found (GH_TOKEN/GITHUB_TOKEN unset and `gh auth token` returned empty).",
                "Run `gh auth login` or export GH_TOKEN.",
                exit_code=78,
            )

    def _request_json(self, url: str) -> tuple[Any, dict[str, str]]:
        token = self._resolve_token()
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cubrid-xref",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return json.loads(raw), headers
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            if exc.code == 401:
                raise CliError(
                    "AUTH_INVALID",
                    "GitHub API rejected credentials (HTTP 401).",
                    "Run `gh auth login` or refresh GH_TOKEN.",
                    exit_code=78,
                ) from exc
            if exc.code in (403, 429):
                is_rate_limit = (
                    exc.code == 429
                    or headers.get("x-ratelimit-remaining") == "0"
                    or "retry-after" in headers
                    or "rate limit" in body_text.lower()
                )
                if is_rate_limit:
                    raise CliError(
                        "RATE_LIMITED",
                        f"GitHub API rate limit reached (HTTP {exc.code}).",
                        "Wait for the rate-limit window to reset and retry.",
                        exit_code=75,
                        transient=True,
                    ) from exc
                raise CliError(
                    "AUTH_FORBIDDEN",
                    f"GitHub API denied access to {url} (HTTP 403).",
                    "Ensure your token has `repo` scope for private CUBRID repositories.",
                    exit_code=78,
                ) from exc
            if exc.code == 404:
                raise CliError(
                    "NOT_FOUND",
                    f"GitHub resource not found: {url}",
                    "Check the repository name and PR number.",
                    exit_code=1,
                ) from exc
            if exc.code >= 500:
                raise CliError(
                    "UPSTREAM_UNAVAILABLE",
                    f"GitHub API server error (HTTP {exc.code}).",
                    "Retry the command in a moment.",
                    exit_code=75,
                    transient=True,
                ) from exc
            raise CliError(
                "GITHUB_ERROR",
                f"GitHub API request failed (HTTP {exc.code}): {body_text[:200]}",
                "Inspect the query and retry.",
                exit_code=1,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CliError(
                "UPSTREAM_UNAVAILABLE",
                f"Failed to reach GitHub API: {exc}",
                "Check network connectivity and retry.",
                exit_code=75,
                transient=True,
            ) from exc

    @staticmethod
    def _pr_from_pull_api(repo: str, item: dict[str, Any]) -> PullRequest:
        title = item.get("title") or ""
        body = item.get("body") or ""
        head_ref = (item.get("head") or {}).get("ref") or ""
        base_ref = (item.get("base") or {}).get("ref") or ""
        primary, mentioned = extract_jira_keys_from_pr(title, body, head_ref)
        return PullRequest(
            repo=repo,
            number=int(item["number"]),
            title=title,
            url=item.get("html_url") or f"https://github.com/{repo}/pull/{item['number']}",
            state=item.get("state") or "open",
            is_draft=bool(item.get("draft", False)),
            merged_at=item.get("merged_at"),
            base_ref=base_ref,
            head_ref=head_ref,
            author=(item.get("user") or {}).get("login") or "",
            created_at=item.get("created_at") or "",
            updated_at=item.get("updated_at") or "",
            role=classify_repo(repo),
            primary_issues=tuple(primary),
            mentioned_issues=tuple(mentioned),
            body=body,
        )

    @staticmethod
    def _pr_from_search_item(item: dict[str, Any]) -> PullRequest:
        repo = _parse_repo_from_api_url(item.get("repository_url") or "")
        title = item.get("title") or ""
        body = item.get("body") or ""
        pr_meta = item.get("pull_request") or {}
        primary, mentioned = extract_jira_keys_from_pr(title, body, "")
        return PullRequest(
            repo=repo,
            number=int(item["number"]),
            title=title,
            url=item.get("html_url") or f"https://github.com/{repo}/pull/{item['number']}",
            state=item.get("state") or "open",
            is_draft=bool(item.get("draft", False)),
            merged_at=pr_meta.get("merged_at"),
            base_ref="",
            head_ref="",
            author=(item.get("user") or {}).get("login") or "",
            created_at=item.get("created_at") or "",
            updated_at=item.get("updated_at") or "",
            role=classify_repo(repo),
            primary_issues=tuple(primary),
            mentioned_issues=tuple(mentioned),
            body=body,
        )

    def get_pull(self, repo: str, number: int) -> PullRequest:
        data, _ = self._request_json(f"https://api.github.com/repos/{repo}/pulls/{number}")
        return self._pr_from_pull_api(repo, data)

    def list_pulls_by_head(self, repo: str, head: str, *, max_pages: int = 5) -> list[PullRequest]:
        encoded_head = urllib.parse.quote(head, safe=":")
        url: str | None = (
            f"https://api.github.com/repos/{repo}/pulls?head={encoded_head}&state=all&per_page=100"
        )
        prs: list[PullRequest] = []
        pages = 0
        while url and pages < max_pages:
            data, headers = self._request_json(url)
            pages += 1
            for item in data:
                prs.append(self._pr_from_pull_api(repo, item))
            link_hdr = headers.get("link", "")
            m = _NEXT_LINK_RE.search(link_hdr)
            url = m.group(1) if m else None
        return prs

    def search_prs(self, query: str, *, max_results: int = 200) -> list[PullRequest]:
        encoded_q = urllib.parse.quote(query, safe="")
        results: list[PullRequest] = []
        page = 1
        while len(results) < max_results:
            per_page = min(100, max_results - len(results))
            url = (
                f"https://api.github.com/search/issues?q={encoded_q}"
                f"&sort=created&order=asc&per_page={per_page}&page={page}"
            )
            data, _ = self._request_json(url)
            items = data.get("items") or []
            for item in items:
                if "pull_request" in item:
                    results.append(self._pr_from_search_item(item))
            total_count = int(data.get("total_count") or 0)
            if len(items) < per_page or page * per_page >= min(total_count, 1000):
                break
            page += 1
        return results


class JiraClient:
    """Thin CUBRID JIRA REST API client using env/~/.netrc credentials or anonymous GET."""

    def __init__(self, *, server: str = DEFAULT_JIRA_SERVER, timeout: float = 15.0) -> None:
        self.server = server.rstrip("/")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._creds_loaded = False
        self._creds: tuple[str, str] | None = None

    def _resolve_credentials(self) -> tuple[str, str] | None:
        with self._lock:
            if self._creds_loaded:
                return self._creds
            user = os.environ.get("CUBRID_JIRA_USER", "").strip()
            pw = os.environ.get("CUBRID_JIRA_PASSWORD", "").strip()
            if user and pw:
                self._creds = (user, pw)
                self._creds_loaded = True
                return self._creds

            host = urllib.parse.urlparse(self.server).hostname or "jira.cubrid.org"
            try:
                nrc = netrc.netrc()
                auth = nrc.authenticators(host)
                if auth and auth[0] and auth[2]:
                    self._creds = (auth[0], auth[2])
            except (FileNotFoundError, netrc.NetrcParseError):
                self._creds = None

            self._creds_loaded = True
            return self._creds

    def _request_json(self, path: str) -> Any:
        url = f"{self.server}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "cubrid-xref",
        }
        creds = self._resolve_credentials()
        if creds is not None:
            raw_tok = f"{creds[0]}:{creds[1]}".encode("utf-8")
            headers["Authorization"] = f"Basic {base64.b64encode(raw_tok).decode('ascii')}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise CliError(
                    "JIRA_AUTH_ERROR",
                    f"JIRA rejected authentication for {url} (HTTP {exc.code}).",
                    "Configure CUBRID_JIRA_USER/CUBRID_JIRA_PASSWORD or ~/.netrc, or pass --no-jira.",
                    exit_code=78,
                ) from exc
            if exc.code == 404:
                raise CliError(
                    "NOT_FOUND",
                    f"JIRA issue not found: {url}",
                    "Verify the issue key (e.g. CBRD-28257).",
                    exit_code=1,
                ) from exc
            if exc.code in (429, 500, 502, 503, 504):
                raise CliError(
                    "UPSTREAM_UNAVAILABLE",
                    f"JIRA API temporarily unavailable (HTTP {exc.code}).",
                    "Retry shortly or pass --no-jira for GitHub-only lookup.",
                    exit_code=75,
                    transient=True,
                ) from exc
            raise CliError(
                "JIRA_ERROR",
                f"JIRA API request failed (HTTP {exc.code}) for {url}.",
                "Verify the issue key or pass --no-jira.",
                exit_code=1,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CliError(
                "UPSTREAM_UNAVAILABLE",
                f"Failed to reach JIRA server {self.server}: {exc}",
                "Check VPN/network access or pass --no-jira for GitHub-only lookup.",
                exit_code=75,
                transient=True,
            ) from exc

    def get_issue(
        self,
        key: str,
        *,
        include_links: bool = True,
    ) -> tuple[JiraIssue, list[RelatedJiraIssue], list[tuple[str, int]]]:
        fields = "summary,status,issuetype,resolution,assignee,fixVersions,parent,subtasks,issuelinks"
        data = self._request_json(f"/rest/api/2/issue/{urllib.parse.quote(key)}?fields={fields}")
        f = data.get("fields") or {}
        issue_key = data.get("key") or key.upper()
        issue = JiraIssue(
            key=issue_key,
            url=f"{self.server}/browse/{issue_key}",
            summary=f.get("summary") or "",
            status=((f.get("status") or {}).get("name")) or "",
            issue_type=((f.get("issuetype") or {}).get("name")) or "",
            resolution=((f.get("resolution") or {}).get("name")) or "Unresolved",
            assignee=((f.get("assignee") or {}).get("displayName")) or "Unassigned",
            fix_versions=tuple(v.get("name", "") for v in (f.get("fixVersions") or []) if v.get("name")),
            found=True,
        )

        related: list[RelatedJiraIssue] = []
        seen_rel: set[tuple[str, str]] = set()

        parent = f.get("parent")
        if parent and parent.get("key"):
            pk = parent["key"]
            pf = parent.get("fields") or {}
            seen_rel.add(("parent", pk))
            related.append(
                RelatedJiraIssue(
                    relation="parent",
                    key=pk,
                    url=f"{self.server}/browse/{pk}",
                    summary=pf.get("summary") or "",
                    status=((pf.get("status") or {}).get("name")) or "",
                )
            )

        for sub in f.get("subtasks") or []:
            sk = sub.get("key")
            if not sk or ("subtask", sk) in seen_rel:
                continue
            sf = sub.get("fields") or {}
            seen_rel.add(("subtask", sk))
            related.append(
                RelatedJiraIssue(
                    relation="subtask",
                    key=sk,
                    url=f"{self.server}/browse/{sk}",
                    summary=sf.get("summary") or "",
                    status=((sf.get("status") or {}).get("name")) or "",
                )
            )

        for link in f.get("issuelinks") or []:
            ltype = link.get("type") or {}
            if "outwardIssue" in link:
                target = link["outwardIssue"]
                rel = ltype.get("outward") or "relates to"
                tk = target.get("key")
                if tk and (rel, tk) not in seen_rel:
                    tf = target.get("fields") or {}
                    seen_rel.add((rel, tk))
                    related.append(
                        RelatedJiraIssue(
                            relation=rel,
                            key=tk,
                            url=f"{self.server}/browse/{tk}",
                            summary=tf.get("summary") or "",
                            status=((tf.get("status") or {}).get("name")) or "",
                        )
                    )
            if "inwardIssue" in link:
                target = link["inwardIssue"]
                rel = ltype.get("inward") or "relates to"
                tk = target.get("key")
                if tk and (rel, tk) not in seen_rel:
                    tf = target.get("fields") or {}
                    seen_rel.add((rel, tk))
                    related.append(
                        RelatedJiraIssue(
                            relation=rel,
                            key=tk,
                            url=f"{self.server}/browse/{tk}",
                            summary=tf.get("summary") or "",
                            status=((tf.get("status") or {}).get("name")) or "",
                        )
                    )

        remotelink_prs: list[tuple[str, int]] = []
        if include_links:
            try:
                rl_data = self._request_json(
                    f"/rest/api/2/issue/{urllib.parse.quote(key)}/remotelink"
                )
                for rl in rl_data or []:
                    obj = rl.get("object") or {}
                    rl_url = obj.get("url") or ""
                    for repo, num in extract_github_pr_urls(rl_url):
                        if repo.lower().startswith("cubrid/") and (repo, num) not in remotelink_prs:
                            remotelink_prs.append((repo, num))
            except CliError:
                pass

        return issue, related, remotelink_prs
