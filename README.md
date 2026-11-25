# cubrid-xref

Bidirectional cross-reference and caching CLI between CUBRID GitHub PRs (`CUBRID/cubrid`, `CUBRID/cubrid-testcases-private-ex`, `CUBRID/cubrid-testcases-private`, `CUBRID/cubrid-testcases`) and CUBRID JIRA issues (`CBRD-*`, etc.).

## Installation

```bash
just sync
```

Or install directly from GitHub:

```bash
uv tool install git+https://github.com/vimkim/cubrid-xref
```

## Usage

Auto-detect PR vs JIRA target:

```bash
cubrid-xref https://github.com/CUBRID/cubrid/pull/8693
cubrid-xref 8693
cubrid-xref cubrid-testcases-private-ex#5098
cubrid-xref CBRD-28257
cubrid-xref https://jira.cubrid.org/browse/CBRD-28257
```

Explicit modes and follow-ups:

```bash
cubrid-xref pr 8693
cubrid-xref pr --repo CUBRID/cubrid-testcases 4400
cubrid-xref jira CBRD-26357 --follow-links
cubrid-xref jira 28257
```

Cache management and refresh:

```bash
cubrid-xref --refresh CBRD-28257     # force live fetch and update cache
cubrid-xref --cache-only CBRD-28257  # read cached entry offline (even if expired)
cubrid-xref --no-cache 8693          # bypass cache read and write
cubrid-xref cache status             # show cache file location, size, and counts
cubrid-xref cache clear --expired    # remove expired entries
cubrid-xref cache clear              # remove all cached entries
```

## Caching

Lookup results are cached locally in an XDG SQLite database (WAL mode):

- Default path: `${CUBRID_XREF_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/cubrid-xref}/cache.sqlite3`
- Permissions: directory `0700`, database file `0600` (cached data may include private TC PR titles/URLs)
- Default TTL: **24 hours** (override with `--ttl HOURS` or `CUBRID_XREF_CACHE_TTL_HOURS`)
- Cross-population: looking up a JIRA issue also caches individual PR entries for single-issue PRs in its bundles, so subsequent `cubrid-xref <PR>` lookups hit the cache immediately.

## Flags

- `--json`: structured JSON output (`{"ok": true, "schema_version": 1, "data": ...}` on stdout; errors on stderr)
- `--plain`: grep/awk-friendly tab-separated rows
- `--no-color`: disable ANSI colors
- `-r`, `--refresh`, `-u`, `--update`: force live fetch and update cache
- `--cache-only`, `--offline`: read from cache without network access
- `--no-cache`: bypass cache read and write
- `--ttl HOURS`: cache TTL in hours (default: `$CUBRID_XREF_CACHE_TTL_HOURS` or `24`)
- `--cache-dir PATH`: override cache directory
- `--no-jira`: skip JIRA API queries (GitHub only)
- `--include-mentions`: include PRs that only mention the issue in their body/comments
- `--follow-links`: resolve PRs for linked/subtask JIRA issues (follow-ups)
- `--max-related N`: limit related JIRA issues resolved with `--follow-links` (default: `10`)
- `--repo OWNER/REPO`: default repository for bare PR numbers (default: `CUBRID/cubrid`)
- `--jira-server URL`: JIRA server base URL (default: `$CUBRID_JIRA_SERVER` or `https://jira.cubrid.org`)
- `--timeout SECONDS`: HTTP timeout in seconds (default: `15`)

## Authentication

- **GitHub**: uses `GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth token`.
- **JIRA**: uses `CUBRID_JIRA_USER` + `CUBRID_JIRA_PASSWORD`, `~/.netrc` for the JIRA host, or anonymous GET fallback.

## Exit Codes

- `0`: success
- `1`: target not found, cache miss (`--cache-only`), or domain failure
- `2`: invalid CLI usage or target syntax
- `75`: temporary upstream/rate-limit failure (retry-safe)
- `78`: missing or invalid authentication/configuration

## Development

```bash
just check
```
