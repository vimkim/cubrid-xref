"""Bidirectional PR <-> JIRA resolution, bundling, and cache integration."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from cubrid_xref.cache import DEFAULT_TTL_SECONDS, XrefCache, format_utc_iso
from cubrid_xref.clients import GitHubClient, JiraClient
from cubrid_xref.models import (
    CacheMeta,
    CliError,
    JiraIssue,
    JiraLookupResult,
    PrBundle,
    PrLookupResult,
    PullRequest,
    RelatedJiraIssue,
)
from cubrid_xref.parse import (
    DEFAULT_JIRA_SERVER,
    ENGINE_REPO,
    TC_PRIVATE_EX_REPO,
    TC_PUBLIC_REPO,
    _BODY_JIRA_BROWSE_RE,
    _extract_keys_from_text,
    extract_engine_pr_refs_from_tc_pr,
    extract_reverted_pr_number,
)


def pr_variant(jira_server: str, fetch_jira: bool) -> str:
    return f"{jira_server.rstrip('/')}|jira={int(fetch_jira)}"


def jira_variant(jira_server: str, fetch_jira: bool, include_mentions: bool) -> str:
    return f"{jira_server.rstrip('/')}|jira={int(fetch_jira)}|mentions={int(include_mentions)}"


def _pr_sort_key(pr: PullRequest) -> tuple[str, int]:
    return (pr.repo.lower(), pr.number)


def _dedupe_sort_prs(prs: list[PullRequest]) -> tuple[PullRequest, ...]:
    by_key: dict[tuple[str, int], PullRequest] = {}
    for pr in prs:
        k = (pr.repo.lower(), pr.number)
        existing = by_key.get(k)
        # Prefer hydrated PR (has head_ref / base_ref) over search stub.
        if existing is None or (not existing.head_ref and pr.head_ref):
            by_key[k] = pr
    return tuple(sorted(by_key.values(), key=_pr_sort_key))


def _is_direct_pr_for_issue(
    issue_key: str,
    pr: PullRequest,
    remotelink_keys: set[tuple[str, int]],
) -> bool:
    if (pr.repo.lower(), pr.number) in remotelink_keys:
        return True
    title_keys = _extract_keys_from_text(
        pr.title,
        case_insensitive_cbrd=True,
        allow_bracket_unknown=True,
    )
    if issue_key in title_keys:
        return True
    if not title_keys:
        top_lines = [line.strip() for line in (pr.body or "").splitlines() if line.strip()][:5]
        for line in top_lines:
            for key in _BODY_JIRA_BROWSE_RE.findall(line):
                if key.upper() == issue_key:
                    return True
    return False


def _fetch_jira_summaries(
    keys: list[str],
    jira: JiraClient,
    *,
    fetch_jira: bool,
    jira_server: str,
) -> tuple[tuple[JiraIssue, ...], list[str]]:
    if not keys:
        return (), []
    base = jira_server.rstrip("/")
    if not fetch_jira:
        return (
            tuple(JiraIssue(key=k, url=f"{base}/browse/{k}", found=False) for k in keys),
            [],
        )

    issues_by_key: dict[str, JiraIssue] = {}
    warnings: list[str] = []

    def _one(k: str) -> tuple[str, JiraIssue | None, str | None]:
        try:
            issue, _, _ = jira.get_issue(k, include_links=False)
            return k, issue, None
        except CliError as exc:
            return k, JiraIssue(key=k, url=f"{base}/browse/{k}", found=False), f"{k}: {exc.message}"

    with ThreadPoolExecutor(max_workers=min(8, len(keys))) as pool:
        for k, issue, warn in pool.map(_one, keys):
            if issue is not None:
                issues_by_key[k] = issue
            if warn:
                warnings.append(warn)

    return tuple(issues_by_key[k] for k in keys), warnings


def _live_cache_meta(
    cache: XrefCache | None,
    *,
    fetched_at: float,
    ttl_seconds: float,
    refresh: bool,
) -> CacheMeta:
    if cache is None:
        source = "live_nocache"
        cache_path = ""
    else:
        source = "live_refreshed" if refresh else "live"
        cache_path = str(cache.path)
    return CacheMeta(
        hit=False,
        fetched_at=format_utc_iso(fetched_at),
        age_seconds=0.0,
        ttl_seconds=float(ttl_seconds),
        stale=False,
        cache_path=cache_path,
        source=source,
    )


def resolve_pr(
    repo: str,
    number: int,
    gh: GitHubClient,
    jira: JiraClient,
    *,
    fetch_jira: bool = True,
    jira_server: str = DEFAULT_JIRA_SERVER,
    cache: XrefCache | None = None,
    refresh: bool = False,
    cache_only: bool = False,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> PrLookupResult:
    """Resolve a GitHub PR to its linked JIRA issues and sibling engine/TC PRs."""
    cache_key = f"{repo.lower()}#{number}"
    variant = pr_variant(jira_server, fetch_jira)

    if cache is not None and not refresh:
        cached = cache.get(
            "pr",
            cache_key,
            variant,
            ttl_seconds=ttl_seconds,
            allow_stale=cache_only,
            now=now,
        )
        if cached is not None:
            payload, meta = cached
            return PrLookupResult.from_dict(payload, cache=meta)
        if cache_only:
            raise CliError(
                "CACHE_MISS",
                f"No cached entry for {repo}#{number}.",
                f"Run `cubrid-xref {repo}#{number}` online to fetch and populate the cache.",
                exit_code=1,
            )
    elif cache_only:
        raise CliError(
            "CACHE_MISS",
            f"Cannot use --cache-only when cache is disabled for {repo}#{number}.",
            "Remove --no-cache or --cache-only.",
            exit_code=2,
        )

    target_pr = gh.get_pull(repo, number)
    warnings: list[str] = []

    engine_pr: PullRequest | None = None
    tc_private: list[PullRequest] = []
    tc_public: list[PullRequest] = []

    def _safe_head_list(tc_repo: str, head_branch: str) -> tuple[list[PullRequest], str | None]:
        try:
            return gh.list_pulls_by_head(tc_repo, f"CUBRID:{head_branch}"), None
        except CliError as exc:
            return [], f"{tc_repo} ({head_branch}): {exc.message}"

    if target_pr.role == "engine":
        engine_pr = target_pr
        head_branch = f"tc/pr-{target_pr.number}"
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_priv = pool.submit(_safe_head_list, TC_PRIVATE_EX_REPO, head_branch)
            f_pub = pool.submit(_safe_head_list, TC_PUBLIC_REPO, head_branch)
            priv_list, priv_warn = f_priv.result()
            pub_list, pub_warn = f_pub.result()
        tc_private.extend(priv_list)
        tc_public.extend(pub_list)
        if priv_warn:
            warnings.append(priv_warn)
        if pub_warn:
            warnings.append(pub_warn)
    elif target_pr.role in ("tc_private", "tc_public"):
        if target_pr.role == "tc_private":
            tc_private.append(target_pr)
        else:
            tc_public.append(target_pr)

        eng_refs = extract_engine_pr_refs_from_tc_pr(
            target_pr.title,
            target_pr.body,
            target_pr.head_ref,
        )
        if eng_refs:
            eng_num = eng_refs[0]
            head_branch = f"tc/pr-{eng_num}"

            def _safe_engine() -> tuple[PullRequest | None, str | None]:
                try:
                    return gh.get_pull(ENGINE_REPO, eng_num), None
                except CliError as exc:
                    return None, f"{ENGINE_REPO}#{eng_num}: {exc.message}"

            with ThreadPoolExecutor(max_workers=3) as pool:
                f_eng = pool.submit(_safe_engine)
                f_priv = pool.submit(_safe_head_list, TC_PRIVATE_EX_REPO, head_branch)
                f_pub = pool.submit(_safe_head_list, TC_PUBLIC_REPO, head_branch)
                engine_pr, eng_warn = f_eng.result()
                priv_list, priv_warn = f_priv.result()
                pub_list, pub_warn = f_pub.result()

            if eng_warn:
                warnings.append(eng_warn)
            if priv_warn:
                warnings.append(priv_warn)
            if pub_warn:
                warnings.append(pub_warn)
            tc_private.extend(priv_list)
            tc_public.extend(pub_list)

    primary_keys = list(target_pr.primary_issues)
    if not primary_keys and engine_pr is not None and engine_pr.primary_issues:
        primary_keys = list(engine_pr.primary_issues)

    mentioned_keys = [k for k in target_pr.mentioned_issues if k not in primary_keys]
    all_keys = primary_keys + mentioned_keys
    enriched, jira_warns = _fetch_jira_summaries(
        all_keys,
        jira,
        fetch_jira=fetch_jira,
        jira_server=jira_server,
    )
    warnings.extend(jira_warns)
    enriched_map = {item.key: item for item in enriched}

    base_result = PrLookupResult(
        target_pr=target_pr,
        primary_issues=tuple(enriched_map[k] for k in primary_keys),
        mentioned_issues=tuple(enriched_map[k] for k in mentioned_keys),
        engine_pr=engine_pr,
        tc_private=_dedupe_sort_prs(tc_private),
        tc_public=_dedupe_sort_prs(tc_public),
        warnings=tuple(warnings),
        cache=None,
    )

    current = time.time() if now is None else float(now)
    if cache is not None and not base_result.warnings:
        cache.put("pr", cache_key, variant, base_result.to_dict(), fetched_at=current)

    return replace(
        base_result,
        cache=_live_cache_meta(
            cache,
            fetched_at=current,
            ttl_seconds=ttl_seconds,
            refresh=refresh,
        ),
    )


def _resolve_followups(
    key: str,
    related_issues: tuple[RelatedJiraIssue, ...],
    gh: GitHubClient,
    jira: JiraClient,
    *,
    fetch_jira: bool,
    include_mentions: bool,
    max_related: int,
    jira_server: str,
    cache: XrefCache | None,
    refresh: bool,
    cache_only: bool,
    ttl_seconds: float,
    now: float | None,
) -> tuple[tuple[JiraLookupResult, ...], list[str]]:
    if not related_issues or max_related <= 0:
        return (), []
    ordered_rel = sorted(
        related_issues,
        key=lambda r: (0 if r.relation == "subtask" else 1, r.key),
    )
    seen_rel_keys: set[str] = {key}
    target_rel_keys: list[str] = []
    for rel in ordered_rel:
        rk = rel.key.upper()
        if rk not in seen_rel_keys:
            seen_rel_keys.add(rk)
            target_rel_keys.append(rk)
            if len(target_rel_keys) >= max_related:
                break

    followups: list[JiraLookupResult] = []
    warnings: list[str] = []
    for rk in target_rel_keys:
        try:
            followups.append(
                resolve_jira(
                    rk,
                    gh,
                    jira,
                    fetch_jira=fetch_jira,
                    include_mentions=include_mentions,
                    follow_links=False,
                    jira_server=jira_server,
                    cache=cache,
                    refresh=refresh,
                    cache_only=cache_only,
                    ttl_seconds=ttl_seconds,
                    now=now,
                )
            )
        except CliError as exc:
            warnings.append(f"follow-up {rk}: {exc.message}")
    return tuple(followups), warnings


def resolve_jira(
    issue_key: str,
    gh: GitHubClient,
    jira: JiraClient,
    *,
    fetch_jira: bool = True,
    include_mentions: bool = False,
    follow_links: bool = False,
    max_related: int = 10,
    jira_server: str = DEFAULT_JIRA_SERVER,
    cache: XrefCache | None = None,
    refresh: bool = False,
    cache_only: bool = False,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> JiraLookupResult:
    """Resolve a JIRA issue key to bundled engine+TC PRs and related issues."""
    key = issue_key.strip().upper()
    base = jira_server.rstrip("/")
    variant = jira_variant(base, fetch_jira, include_mentions)

    if cache is not None and not refresh:
        cached = cache.get(
            "jira",
            key,
            variant,
            ttl_seconds=ttl_seconds,
            allow_stale=cache_only,
            now=now,
        )
        if cached is not None:
            payload, meta = cached
            base_result = JiraLookupResult.from_dict(payload, cache=meta)
            if follow_links and base_result.related_issues and max_related > 0:
                followups, extra_warns = _resolve_followups(
                    key,
                    base_result.related_issues,
                    gh,
                    jira,
                    fetch_jira=fetch_jira,
                    include_mentions=include_mentions,
                    max_related=max_related,
                    jira_server=base,
                    cache=cache,
                    refresh=refresh,
                    cache_only=cache_only,
                    ttl_seconds=ttl_seconds,
                    now=now,
                )
                return replace(
                    base_result,
                    followups=followups,
                    warnings=base_result.warnings + tuple(extra_warns),
                )
            return base_result
        if cache_only:
            raise CliError(
                "CACHE_MISS",
                f"No cached entry for {key}.",
                f"Run `cubrid-xref {key}` online to fetch and populate the cache.",
                exit_code=1,
            )
    elif cache_only:
        raise CliError(
            "CACHE_MISS",
            f"Cannot use --cache-only when cache is disabled for {key}.",
            "Remove --no-cache or --cache-only.",
            exit_code=2,
        )

    warnings: list[str] = []

    def _safe_jira() -> tuple[
        JiraIssue | None,
        list[RelatedJiraIssue],
        list[tuple[str, int]],
        CliError | None,
    ]:
        if not fetch_jira:
            return (
                JiraIssue(key=key, url=f"{base}/browse/{key}", found=False),
                [],
                [],
                None,
            )
        try:
            iss, rel, rlinks = jira.get_issue(key, include_links=True)
            return iss, rel, rlinks, None
        except CliError as exc:
            return None, [], [], exc

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_jira = pool.submit(_safe_jira)
        f_title = pool.submit(gh.search_prs, f'org:CUBRID is:pr "{key}" in:title')
        f_body = pool.submit(gh.search_prs, f'org:CUBRID is:pr "browse/{key}" in:body')
        jira_issue, related_issues, remotelinks, jira_err = f_jira.result()
        title_prs = f_title.result()
        body_prs = f_body.result()

    # Merge GitHub search candidates
    candidates: dict[tuple[str, int], PullRequest] = {}
    for pr in title_prs + body_prs:
        candidates[(pr.repo.lower(), pr.number)] = pr

    remotelink_keys = {(repo.lower(), num) for repo, num in remotelinks}

    if jira_err is not None:
        if jira_err.code == "NOT_FOUND" and not candidates and not remotelink_keys:
            raise jira_err
        warnings.append(f"{key}: {jira_err.message}")
        jira_issue = JiraIssue(key=key, url=f"{base}/browse/{key}", found=False)
    assert jira_issue is not None

    # Hydrate any remotelink PRs missing from GitHub search
    missing_remotelinks = [
        (repo, num)
        for repo, num in remotelinks
        if (repo.lower(), num) not in candidates
    ]
    if missing_remotelinks:
        def _fetch_rl(item: tuple[str, int]) -> tuple[PullRequest | None, str | None]:
            try:
                return gh.get_pull(item[0], item[1]), None
            except CliError as exc:
                return None, f"{item[0]}#{item[1]}: {exc.message}"

        with ThreadPoolExecutor(max_workers=min(8, len(missing_remotelinks))) as pool:
            for pr, warn in pool.map(_fetch_rl, missing_remotelinks):
                if pr is not None:
                    candidates[(pr.repo.lower(), pr.number)] = pr
                if warn:
                    warnings.append(warn)

    direct_prs: list[PullRequest] = []
    mention_prs: list[PullRequest] = []
    for pr in candidates.values():
        if _is_direct_pr_for_issue(key, pr, remotelink_keys):
            direct_prs.append(pr)
        else:
            mention_prs.append(pr)

    direct_engines = [pr for pr in direct_prs if pr.role == "engine"]
    direct_tcs = [pr for pr in direct_prs if pr.role in ("tc_private", "tc_public")]
    direct_others = [pr for pr in direct_prs if pr.role == "other"]

    # Hydrate direct engine PRs + query tc/pr-<N> in both TC repos
    hydrated_engines: dict[int, PullRequest] = {}
    bundle_tc_priv: dict[int, list[PullRequest]] = {pr.number: [] for pr in direct_engines}
    bundle_tc_pub: dict[int, list[PullRequest]] = {pr.number: [] for pr in direct_engines}
    attached_tc_keys: set[tuple[str, int]] = set()

    if direct_engines:
        def _hydrate_engine_and_tcs(
            eng_stub: PullRequest,
        ) -> tuple[PullRequest, list[PullRequest], list[PullRequest], list[str]]:
            local_warns: list[str] = []
            try:
                eng = gh.get_pull(eng_stub.repo, eng_stub.number)
            except CliError as exc:
                eng = eng_stub
                local_warns.append(f"{eng_stub.short_ref}: {exc.message}")

            head_branch = f"tc/pr-{eng.number}"
            priv: list[PullRequest] = []
            pub: list[PullRequest] = []
            try:
                priv = gh.list_pulls_by_head(TC_PRIVATE_EX_REPO, f"CUBRID:{head_branch}")
            except CliError as exc:
                local_warns.append(f"{TC_PRIVATE_EX_REPO} ({head_branch}): {exc.message}")
            try:
                pub = gh.list_pulls_by_head(TC_PUBLIC_REPO, f"CUBRID:{head_branch}")
            except CliError as exc:
                local_warns.append(f"{TC_PUBLIC_REPO} ({head_branch}): {exc.message}")
            return eng, priv, pub, local_warns

        with ThreadPoolExecutor(max_workers=min(8, len(direct_engines))) as pool:
            for eng, priv, pub, lwarns in pool.map(_hydrate_engine_and_tcs, direct_engines):
                hydrated_engines[eng.number] = eng
                warnings.extend(lwarns)
                for p in priv:
                    bundle_tc_priv[eng.number].append(p)
                    attached_tc_keys.add((p.repo.lower(), p.number))
                for p in pub:
                    bundle_tc_pub[eng.number].append(p)
                    attached_tc_keys.add((p.repo.lower(), p.number))

    # Hydrate remaining direct TC PRs (critical for head_ref such as revert-3551-tc/pr-7990)
    unattached_tcs = [
        pr for pr in direct_tcs if (pr.repo.lower(), pr.number) not in attached_tc_keys
    ]
    hydrated_unattached_tcs: list[PullRequest] = []
    if unattached_tcs:
        def _hydrate_tc(tc_stub: PullRequest) -> tuple[PullRequest, str | None]:
            if tc_stub.head_ref:
                return tc_stub, None
            try:
                return gh.get_pull(tc_stub.repo, tc_stub.number), None
            except CliError as exc:
                return tc_stub, f"{tc_stub.short_ref}: {exc.message}"

        with ThreadPoolExecutor(max_workers=min(8, len(unattached_tcs))) as pool:
            for tc_pr, warn in pool.map(_hydrate_tc, unattached_tcs):
                hydrated_unattached_tcs.append(tc_pr)
                if warn:
                    warnings.append(warn)

    # First pass: attach TC PRs whose head/title/body points to a bundled engine PR
    still_standalone: list[PullRequest] = []
    for tc_pr in hydrated_unattached_tcs:
        refs = extract_engine_pr_refs_from_tc_pr(tc_pr.title, tc_pr.body, tc_pr.head_ref)
        matched_eng = next((n for n in refs if n in hydrated_engines), None)
        if matched_eng is not None:
            if tc_pr.role == "tc_private":
                bundle_tc_priv[matched_eng].append(tc_pr)
            else:
                bundle_tc_pub[matched_eng].append(tc_pr)
            attached_tc_keys.add((tc_pr.repo.lower(), tc_pr.number))
        else:
            still_standalone.append(tc_pr)

    # Second pass: attach Revert TC PRs whose reverted PR is already in a bundle
    pr_to_bundle: dict[tuple[str, int], int] = {}
    for eng_num in hydrated_engines:
        for tc_pr in bundle_tc_priv[eng_num] + bundle_tc_pub[eng_num]:
            pr_to_bundle[(tc_pr.repo.lower(), tc_pr.number)] = eng_num

    final_standalone_priv: list[PullRequest] = []
    final_standalone_pub: list[PullRequest] = []
    for tc_pr in still_standalone:
        rev_num = extract_reverted_pr_number(tc_pr.title)
        target_eng = pr_to_bundle.get((tc_pr.repo.lower(), rev_num)) if rev_num else None
        if target_eng is not None:
            if tc_pr.role == "tc_private":
                bundle_tc_priv[target_eng].append(tc_pr)
            else:
                bundle_tc_pub[target_eng].append(tc_pr)
            attached_tc_keys.add((tc_pr.repo.lower(), tc_pr.number))
        elif tc_pr.role == "tc_private":
            final_standalone_priv.append(tc_pr)
        else:
            final_standalone_pub.append(tc_pr)

    bundles = tuple(
        PrBundle(
            engine=hydrated_engines[eng_num],
            tc_private=_dedupe_sort_prs(bundle_tc_priv[eng_num]),
            tc_public=_dedupe_sort_prs(bundle_tc_pub[eng_num]),
        )
        for eng_num in sorted(hydrated_engines)
    )

    base_result = JiraLookupResult(
        issue=jira_issue,
        bundles=bundles,
        standalone_tc_private=_dedupe_sort_prs(final_standalone_priv),
        standalone_tc_public=_dedupe_sort_prs(final_standalone_pub),
        other_prs=_dedupe_sort_prs(direct_others),
        related_issues=tuple(related_issues),
        mentions=_dedupe_sort_prs(mention_prs) if include_mentions else (),
        followups=(),
        warnings=tuple(warnings),
        cache=None,
    )

    current = time.time() if now is None else float(now)
    if cache is not None and not base_result.warnings:
        to_store: list[tuple[str, str, str, dict[str, Any]]] = [
            ("jira", key, variant, base_result.to_dict()),
        ]
        if fetch_jira and jira_issue.found:
            pv = pr_variant(base, True)
            for bundle in bundles:
                eng = bundle.engine
                if eng.primary_issues == (key,) and not eng.mentioned_issues:
                    synth_eng = PrLookupResult(
                        target_pr=eng,
                        primary_issues=(jira_issue,),
                        mentioned_issues=(),
                        engine_pr=eng,
                        tc_private=bundle.tc_private,
                        tc_public=bundle.tc_public,
                    )
                    to_store.append(("pr", f"{eng.repo.lower()}#{eng.number}", pv, synth_eng.to_dict()))
                for tc in bundle.tc_private + bundle.tc_public:
                    if tc.primary_issues in ((), (key,)) and not tc.mentioned_issues:
                        synth_tc = PrLookupResult(
                            target_pr=tc,
                            primary_issues=(jira_issue,),
                            mentioned_issues=(),
                            engine_pr=eng,
                            tc_private=bundle.tc_private,
                            tc_public=bundle.tc_public,
                        )
                        to_store.append(("pr", f"{tc.repo.lower()}#{tc.number}", pv, synth_tc.to_dict()))
        cache.put_many(to_store, fetched_at=current)

    followups: tuple[JiraLookupResult, ...] = ()
    followup_warns: list[str] = []
    if follow_links and related_issues and max_related > 0:
        followups, followup_warns = _resolve_followups(
            key,
            base_result.related_issues,
            gh,
            jira,
            fetch_jira=fetch_jira,
            include_mentions=include_mentions,
            max_related=max_related,
            jira_server=base,
            cache=cache,
            refresh=refresh,
            cache_only=cache_only,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    return replace(
        base_result,
        followups=followups,
        warnings=base_result.warnings + tuple(followup_warns),
        cache=_live_cache_meta(
            cache,
            fetched_at=current,
            ttl_seconds=ttl_seconds,
            refresh=refresh,
        ),
    )
