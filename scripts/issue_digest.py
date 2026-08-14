"""Issue digest for the charmed-hpc org.

A standalone companion to `pr_digest.py`. Reads `repos.yaml`,
queries the GitHub issues endpoint for each repo, filters to open
issues with priority labels (P-critical, P-high, P-medium), and
posts a weekly markdown digest to Mattermost.

The script is intentionally independent from `pr_digest.py`:
- Issues don't have the same review-cycle urgency as PRs, so
  no business-hours staleness or bucketing logic.
- Issues aren't authored by bots in a meaningful way, so no
  EXCLUDE_BOTS / INCLUDE_ONLY_BOTS filters.
- The issues endpoint returns both issues and PRs — PRs are
  filtered out client-side (they have a `pull_request` key).

Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 30
BODY_EXCERPT_CHARS = 200

# Priority labels we want to highlight in the digest. Exact match —
# P-low is intentionally excluded, and any future P-* labels added
# to repos won't be silently swept in.
PRIORITY_LABELS = ("P-critical", "P-high", "P-medium")
# Render order: most urgent first so the recency-critical items
# jump out at the top of the digest.
PRIORITY_ORDER = ("P-critical", "P-high", "P-medium")

# Truthy values accepted for boolean env vars (DRY_RUN).
_TRUTHY = frozenset({"1", "true", "True", "yes", "Yes", "on", "On"})


def _truthy(name: str, default: str = "") -> bool:
    """Read an env var and return True iff it parses as truthy."""
    raw = os.environ.get(name, default)
    return str(raw).strip() in _TRUTHY


def http_get(url: str, token: str) -> Any:
    """GET a JSON resource from the GitHub API. Raises on non-2xx."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "issue-digest",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp from the GitHub API into aware UTC datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def load_repos(path: Path) -> list[str]:
    """Load the list of repos from repos.yaml.

    Unlike pr_digest.py, this script doesn't consume the
    `thresholds` block (issues have no bucketing thresholds), so
    we just return the repo name list. The full mapping is still
    read so a malformed file fails loudly at startup.
    """
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("repos.yaml: top-level must be a mapping")
    repos = data.get("repos", [])
    if not isinstance(repos, list):
        raise ValueError(f"repos.yaml: 'repos' must be a list, got {type(repos).__name__}")
    return [str(r).strip() for r in repos if str(r).strip()]


def list_open_issues(owner: str, repo: str, token: str) -> list[dict[str, Any]]:
    """List all open issues in a repo.

    The GitHub issues endpoint returns both issues and pull
    requests — PRs are filtered out client-side by checking for
    the `pull_request` key (present only on PRs). We use `filter=all`
    (the default) rather than `filter=issues` because the latter
    is not honored by some implementations anyway, and the
    client-side check is cheap.

    Results are returned by the API in created-desc order; the
    caller re-sorts after priority bucketing.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues?state=open&per_page=100"
    data = http_get(url, token)
    if not isinstance(data, list):
        return []
    # Drop PRs returned by the issues endpoint.
    return [issue for issue in data if "pull_request" not in issue]


def has_priority_label(issue: dict[str, Any]) -> bool:
    """Return True if `issue` has at least one target priority label."""
    for label in issue.get("labels", []) or []:
        name = label.get("name", "")
        if name in PRIORITY_LABELS:
            return True
    return False


def collect_priority_issues(
    repo_full: str, token: str
) -> tuple[str, list[dict[str, Any]]] | None:
    """Fetch open priority (P-medium/high/critical) issues for a repo.

    Returns (repo_full, issues) or None on total failure. PRs that
    leak through the issues endpoint are dropped first; then
    issues with no qualifying priority label are dropped. The
    caller renders the result directly — no extra enrichment
    (assignees, milestone, body excerpt) is fetched because the
    list endpoint already includes them.
    """
    if "/" not in repo_full:
        print(f"  ! skipping malformed entry: {repo_full!r}", file=sys.stderr)
        return None
    owner, repo = repo_full.split("/", 1)
    try:
        raw_issues = list_open_issues(owner, repo, token)
    except urllib.error.HTTPError as e:
        print(
            f"  ! {repo_full}: HTTP {e.code} listing issues — skipping",
            file=sys.stderr,
        )
        return None
    except urllib.error.URLError as e:
        print(
            f"  ! {repo_full}: network error ({e.reason}) — skipping",
            file=sys.stderr,
        )
        return None

    matched = [issue for issue in raw_issues if has_priority_label(issue)]
    return (repo_full, matched)


def _esc(s: str) -> str:
    """Escape pipe characters and newlines in a markdown table cell."""
    return s.replace("|", "\\|").replace("\n", " ")


def _body_excerpt(body: str | None) -> str:
    """Return a short single-line excerpt of an issue body.

    Issues often use a template with a "Summary" / "## Summary of
    changes" header. If we find one, the excerpt starts at the
    next non-empty line. Otherwise we use the whole body. Empty
    bodies return empty string so the caller can render the
    "_(no description)_" placeholder.
    """
    if not body:
        return ""
    lines = body.splitlines()
    summary_start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip().rstrip(":").strip()
        if stripped and stripped[:7].lower() == "summary":
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    summary_start = j
                    break
            break
    relevant = lines[summary_start:] if summary_start is not None else lines
    flat = " ".join(l.strip() for l in relevant if l.strip())
    if not flat:
        flat = " ".join(l.strip() for l in lines if l.strip())
    if not flat:
        return ""
    if len(flat) <= BODY_EXCERPT_CHARS:
        return flat
    return flat[: BODY_EXCERPT_CHARS - 1].rstrip() + "…"


def _labels_cell(issue: dict[str, Any]) -> str:
    """Render the full label list as a comma-separated string.

    We render all labels (not just the priority ones) so the
    reader can see context like `bug`, `C-slurm`, or release
    targets alongside the priority. The priority label is
    already implied by the section the issue appears in.
    """
    labels = issue.get("labels") or []
    if not labels:
        return ""
    return ", ".join(
        f"`{l['name']}`" for l in labels if l.get("name")
    )


def _assignees_cell(issue: dict[str, Any]) -> str:
    """Render assignees as a comma-separated list. Empty → '_(none)_'."""
    assignees = issue.get("assignees") or []
    if not assignees:
        return "_(none)_"
    return ", ".join(a["login"] for a in assignees if a.get("login"))


def age_human(created: datetime, now: datetime) -> str:
    """Render an issue's age as a short human string (e.g. '12d03h', '5h')."""
    delta = now - created
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return f"{days}d{hours:02d}h"
    return f"{hours}h"


def last_activity_human(last: datetime, now: datetime) -> str:
    """Render 'time since last activity' as a short human string."""
    delta = now - last
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    if days > 0:
        return f"{days}d{hours:02d}h ago"
    if hours > 0:
        return f"{hours}h{minutes:02d}m ago"
    return f"{minutes}m ago"


def build_issue_row(issue: dict[str, Any], repo_full: str, now: datetime) -> str:
    """Render a single issue as a markdown table row."""
    number = issue["number"]
    url = issue["html_url"]
    author = issue.get("user", {}).get("login", "")
    title = issue["title"]
    if len(title) > 50:
        title = title[:50] + "…"
    repo_short = repo_full.split("/", 1)[-1]
    created_at = parse_iso(issue["created_at"])
    updated_at = parse_iso(issue["updated_at"])
    age = age_human(created_at, now)
    last = last_activity_human(updated_at, now)
    labels_cell = _labels_cell(issue)
    assignees_cell = _assignees_cell(issue)
    excerpt = _body_excerpt(issue.get("body"))
    body_cell = _esc(excerpt) or "_(no description)_"

    return (
        f"| [{repo_short}#{number}]({url}) {title} "
        f"| {author} | {age} | {last} | {labels_cell} | {assignees_cell} "
        f"| {body_cell} |"
    )


def build_issue_section(
    title: str, repo_issues: list[tuple[str, list[dict[str, Any]]]], now: datetime
) -> str:
    """Render a markdown section (heading + table) for a priority bucket.

    `repo_issues` is a list of (repo_full, issues) pairs; we
    flatten across repos and sort by creation date ascending so
    the oldest (most overdue) issue sits at the top. Returns an
    empty string when there are no issues, so the caller can skip
    the heading and table entirely.
    """
    flat: list[tuple[str, dict[str, Any]]] = []
    for repo_full, issues in repo_issues:
        for issue in issues:
            flat.append((repo_full, issue))
    if not flat:
        return ""
    flat.sort(key=lambda ri: parse_iso(ri[1]["created_at"]))

    lines = [f"## {title} ({len(flat)})", ""]
    lines.append("| Issue | Author | Age | Last activity | Labels | Assignees | Description |")
    lines.append("|---|---|---|---|---|---|---|")
    for repo_full, issue in flat:
        lines.append(build_issue_row(issue, repo_full, now))
    lines.append("")
    return "\n".join(lines)


def build_issue_digest(
    repos_with_issues: list[tuple[str, list[dict[str, Any]]]],
    now: datetime,
) -> str:
    """Build the full markdown digest string, grouped by priority bucket.

    All issues across repos are bucketed by their priority label
    (P-critical → P-high → P-medium). Within each bucket, issues
    are sorted oldest-first so the most overdue work surfaces at
    the top. The Org summary lists total counts per priority
    level so the reader can see the landscape at a glance.
    """
    flat: list[tuple[str, dict[str, Any]]] = []
    for repo_full, issues in repos_with_issues:
        for issue in issues:
            flat.append((repo_full, issue))

    today = now.strftime("%Y-%m-%d")
    lines: list[str] = [f"# Issue Digest — charmed-hpc — {today}", ""]

    if not flat:
        lines.append("_No open priority issues across tracked repos. :tada:_")
        lines.append("")
        return "\n".join(lines)

    repo_count = len({repo_full for repo_full, _ in flat})
    total = len(flat)

    # Group by priority label. An issue with multiple priority
    # labels is bucketed by the highest-priority one (P-critical
    # wins over P-high wins over P-medium) — we only render it
    # once, in the most urgent bucket.
    by_priority: dict[str, list[tuple[str, dict[str, Any]]]] = {
        label: [] for label in PRIORITY_ORDER
    }
    for repo_full, issue in flat:
        labels = {l.get("name", "") for l in (issue.get("labels") or [])}
        for label in PRIORITY_ORDER:
            if label in labels:
                by_priority[label].append((repo_full, issue))
                break

    lines.append("## Org summary")
    lines.append(
        f"- {total} open priority issue{'s' if total != 1 else ''} across {repo_count} "
        f"repo{'s' if repo_count != 1 else ''}"
    )
    for label in PRIORITY_ORDER:
        n = len(by_priority[label])
        lines.append(f"- {label}: {n}")
    lines.append("")

    for label in PRIORITY_ORDER:
        section = build_issue_section(label, [("", by_priority[label])], now)
        if section:
            lines.append(section.rstrip())
            lines.append("")

    return "\n".join(lines)


def post_to_mattermost(webhook_url: str, text: str) -> None:
    """POST the digest text to a Mattermost incoming webhook."""
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "issue-digest"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 300:
            print(
                f"Mattermost webhook error response body: {body[:300]}",
                file=sys.stderr,
            )
            raise RuntimeError(
                f"Mattermost webhook returned {resp.status} "
                "(see GitHub Actions logs for full error details)"
            )


def main() -> int:
    """Entry point.

    Required env vars:
      GH_TOKEN                  fine-grained GitHub PAT
      MATTERMOST_WEBHOOK_URL    Mattermost incoming webhook URL

    Optional env vars:
      DRY_RUN        truthy = log digest instead of posting
      REPOS_FILE     path to repos.yaml (default: ./repos.yaml)
    """
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("GH_TOKEN env var is required", file=sys.stderr)
        return 2

    webhook_url = os.environ.get("MATTERMOST_WEBHOOK_URL")
    if not webhook_url:
        print("MATTERMOST_WEBHOOK_URL env var is required", file=sys.stderr)
        return 2

    repos_path = Path(
        os.environ.get("REPOS_FILE", Path(__file__).parent.parent / "repos.yaml")
    )
    repos = load_repos(repos_path)
    if not repos:
        print(f"No repos listed in {repos_path}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    print(f"Fetching open priority issues from {len(repos)} repos…", file=sys.stderr)
    results: list[tuple[str, list[dict[str, Any]]]] = []
    for r in repos:
        print(f"  - {r}", file=sys.stderr)
        result = collect_priority_issues(r, token)
        if result is not None and result[1]:
            results.append(result)

    digest = build_issue_digest(results, now)

    if _truthy("DRY_RUN"):
        print(
            "---- DRY RUN: not posting to Mattermost ----",
            file=sys.stderr,
        )
        print(digest)
        return 0

    print("Posting to Mattermost…", file=sys.stderr)
    post_to_mattermost(webhook_url, digest)
    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
