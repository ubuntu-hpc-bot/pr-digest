"""Daily PR digest for the charmed-hpc org, posted to Mattermost.

Reads repos.yaml, queries the GitHub API for open PRs in each repo,
computes reviewer load and business-hour staleness, and posts a
single combined markdown digest to a Mattermost incoming webhook.

Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from business_hours import business_hours_between


GITHUB_API = "https://api.github.com"
STALE_THRESHOLD_HOURS = 24.0
HTTP_TIMEOUT = 30


def http_get(url: str, token: str) -> dict[str, Any] | list[Any]:
    """GET a JSON resource from the GitHub API. Raises on non-2xx."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-digest",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_optional(url: str, token: str) -> dict[str, Any] | list[Any] | None:
    """GET a JSON resource; return None on 404."""
    try:
        return http_get(url, token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp from the GitHub API into aware UTC datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def load_repos(path: Path) -> tuple[list[str], dict[str, Any]]:
    """Load the list of repos and activity thresholds from repos.yaml."""
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("repos.yaml: top-level must be a mapping")
    repos = data.get("repos", [])
    if not isinstance(repos, list):
        raise ValueError(f"repos.yaml: 'repos' must be a list, got {type(repos).__name__}")
    repo_list = [str(r).strip() for r in repos if str(r).strip()]

    raw_thresholds = data.get("thresholds", {}) or {}
    if not isinstance(raw_thresholds, dict):
        raise ValueError("repos.yaml: 'thresholds' must be a mapping")
    thresholds: dict[str, Any] = {
        "new_max_hours": float(raw_thresholds.get("new_max_hours", 48)),
        "new_max_comments": int(raw_thresholds.get("new_max_comments", 1)),
        "stale_min_hours": float(raw_thresholds.get("stale_min_hours", 120)),
    }
    return repo_list, thresholds


def list_open_prs(owner: str, repo: str, token: str) -> list[dict[str, Any]]:
    """List open PRs in a repo."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls?state=open&per_page=100&sort=updated&direction=desc"
    data = http_get(url, token)
    return data if isinstance(data, list) else []


def list_merged_prs_since(
    owner: str, repo: str, token: str, since: datetime
) -> list[dict[str, Any]]:
    """List PRs merged at or after `since` (UTC) in a repo.

    The GitHub API doesn't support a `since` filter on the pulls
    listing, so we fetch closed PRs (the only way to get merged_at)
    and filter client-side. PRs are returned in updated-desc order
    by the API; we re-sort by merged_at desc so the caller doesn't
    have to.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls?state=closed&per_page=100&sort=updated&direction=desc"
    data = http_get(url, token)
    if not isinstance(data, list):
        return []
    merged: list[dict[str, Any]] = []
    for pr in data:
        merged_at_raw = pr.get("merged_at")
        if not merged_at_raw:
            continue
        merged_at = parse_iso(merged_at_raw)
        if merged_at < since:
            continue
        merged.append(pr)
    merged.sort(key=lambda p: parse_iso(p["merged_at"]), reverse=True)
    return merged


def get_pr_detail(owner: str, repo: str, number: int, token: str) -> dict[str, Any] | None:
    """Fetch full PR detail, including requested_reviewers."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}"
    return http_get_optional(url, token)


def get_pr_comments(owner: str, repo: str, number: int, token: str) -> list[dict[str, Any]]:
    """Fetch issue comments on a PR."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments?per_page=100&sort=created&direction=desc"
    data = http_get_optional(url, token)
    return data if isinstance(data, list) else []


def get_pr_reviews(owner: str, repo: str, number: int, token: str) -> list[dict[str, Any]]:
    """Fetch reviews on a PR."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100"
    data = http_get_optional(url, token)
    return data if isinstance(data, list) else []


def collect_pr_activity(pr: dict[str, Any], token: str) -> dict[str, Any]:
    """Collect enriched PR info: reviewers, last activity, status."""
    owner = pr["base"]["repo"]["owner"]["login"]
    repo = pr["base"]["repo"]["name"]
    number = pr["number"]

    detail = get_pr_detail(owner, repo, number, token) or {}
    comments = get_pr_comments(owner, repo, number, token)
    reviews = get_pr_reviews(owner, repo, number, token)

    requested_reviewers = [r["login"] for r in detail.get("requested_reviewers", [])]
    requested_teams = [t["slug"] for t in detail.get("requested_teams", [])]

    candidates: list[datetime] = [parse_iso(pr["updated_at"])]
    for c in comments:
        candidates.append(parse_iso(c["created_at"]))
    for r in reviews:
        if r.get("submitted_at"):
            candidates.append(parse_iso(r["submitted_at"]))
    last_activity = max(candidates)

    # Issue comments + review bodies count as "comments" for activity.
    review_bodies = [r for r in reviews if r.get("body")]
    comment_count = len(comments) + len(review_bodies)

    # Commenters: include the author too, for display in the digest. The
    # activity/bucketing logic (see external_comment_count below) only
    # counts non-author comments, so a PR where the author is the only
    # commenter still gets flagged as needing attention.
    author_login = pr["user"]["login"]
    commenter_counts: dict[str, int] = {}
    for c in comments:
        u = c.get("user", {}).get("login")
        if u:
            commenter_counts[u] = commenter_counts.get(u, 0) + 1
    for r in review_bodies:
        u = r.get("user", {}).get("login")
        if u:
            commenter_counts[u] = commenter_counts.get(u, 0) + 1
    top_commenters_all = sorted(
        commenter_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )[:3]
    external_comment_count = sum(
        n for u, n in commenter_counts.items() if u != author_login
    )
    external_participant_count = sum(
        1 for u in commenter_counts if u != author_login
    )

    return {
        "number": number,
        "title": pr["title"],
        "author": author_login,
        "html_url": pr["html_url"],
        "repo_full": f"{owner}/{repo}",
        "created_at": parse_iso(pr["created_at"]),
        "updated_at": parse_iso(pr["updated_at"]),
        "last_activity": last_activity,
        "requested_reviewers": requested_reviewers,
        "requested_teams": requested_teams,
        "draft": bool(pr.get("draft", False)),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "review_state": derive_review_state(detail, reviews),
        "comment_count": comment_count,
        "external_comment_count": external_comment_count,
        "external_participant_count": external_participant_count,
        "top_commenters_all": top_commenters_all,
    }


def derive_review_state(detail: dict[str, Any], reviews: list[dict[str, Any]]) -> str:
    """Summarize the latest review state on a PR."""
    latest_state = None
    latest_submitted = None
    for r in reviews:
        submitted = r.get("submitted_at")
        if not submitted:
            continue
        ts = parse_iso(submitted)
        if latest_submitted is None or ts > latest_submitted:
            latest_submitted = ts
            latest_state = r.get("state", "").lower()
    if not detail.get("requested_reviewers") and not detail.get("requested_teams"):
        return "Awaiting author" if latest_state in ("approved", "changes_requested", "commented") else "Ready"
    if latest_state == "approved":
        return "Approved"
    if latest_state == "changes_requested":
        return "Changes requested"
    return "Review required"


def is_stale(last_activity: datetime, now: datetime) -> bool:
    """A PR is stale when no business-hour activity in the last 24h."""
    return business_hours_between(last_activity, now) >= STALE_THRESHOLD_HOURS


def bucket_pr(
    pr: dict[str, Any], now: datetime, thresholds: dict[str, Any]
) -> str:
    """Classify a PR into one of: 'needs_attention', 'stale', 'active'.

    - needs_attention: PR is recent (within new_max_hours) and has very
      little engagement (≤ new_max_comments comments). New but quiet.
    - stale: PR has had no business-hour activity for ≥ stale_min_hours.
    - active: everything else.
    """
    hours_since_activity = business_hours_between(pr["last_activity"], now)
    age_hours = business_hours_between(pr["created_at"], now)

    if (
        age_hours <= thresholds["new_max_hours"]
        and pr["external_comment_count"] <= thresholds["new_max_comments"]
    ):
        return "needs_attention"
    if hours_since_activity >= thresholds["stale_min_hours"]:
        return "stale"
    return "active"


def age_human(created: datetime, now: datetime) -> str:
    """Render a PR's age as a short human string."""
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


def build_pr_row(pr: dict[str, Any], now: datetime) -> str:
    """Render a single PR as a markdown table row."""
    number = pr["number"]
    url = pr["html_url"]
    author = pr["author"]
    repo_short = pr["repo_full"].split("/", 1)[-1]
    age = age_human(pr["created_at"], now)
    last = last_activity_human(pr["last_activity"], now)

    reviewer_list = list(pr["requested_reviewers"])
    if pr["requested_teams"]:
        reviewer_list = reviewer_list + [f"@{t}" for t in pr["requested_teams"]]
    if pr["draft"]:
        reviewers = "_draft_"
    elif reviewer_list:
        reviewers = ", ".join(f"@{r}" for r in reviewer_list)
    else:
        reviewers = "_(none)_"

    comments_cell = (
        f"{pr['comment_count']} ({pr['external_participant_count']} ppl)"
    )
    if pr["top_commenters_all"]:
        author_login = pr["author"]
        rendered = []
        for u, n in pr["top_commenters_all"]:
            tag = "author" if u == author_login else u
            rendered.append(f"@{tag}({n})")
        top = ", ".join(rendered)
        comments_cell = f"{comments_cell} — top: {top}"

    title = pr["title"]
    if len(title) > 50:
        title = title[:50] + "…"
    return (
        f"| [{repo_short}#{number}]({url}) {title} "
        f"| @{author} | {age} | {last} | {comments_cell} | {reviewers} | {pr['review_state']} |"
    )


def build_pr_section(
    title: str, prs: list[dict[str, Any]], now: datetime
) -> str:
    """Render a markdown section (heading + table) for a list of PRs.

    Returns an empty string when there are no PRs, so the caller can
    skip the heading and table entirely.
    """
    if not prs:
        return ""
    lines = [f"## {title} ({len(prs)})", ""]
    lines.append("| PR | Author | Age | Last activity | Comments | Reviewers | Status |")
    lines.append("|---|---|---|---|---|---|---|")
    for pr in prs:
        lines.append(build_pr_row(pr, now))
    lines.append("")
    return "\n".join(lines)


def build_reviewer_load(all_prs: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, int]]:
    """Aggregate open-PR reviewer count per user, sorted descending."""
    counts: dict[str, int] = {}
    for _repo, pr in all_prs:
        for r in pr["requested_reviewers"]:
            counts[r] = counts.get(r, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def build_digest(
    repos_with_prs: list[tuple[str, list[dict[str, Any]]]],
    now: datetime,
    thresholds: dict[str, Any],
) -> str:
    """Build the full markdown digest string, bucketed by activity tier."""
    all_prs: list[dict[str, Any]] = []
    for _repo, prs in repos_with_prs:
        all_prs.extend(prs)

    today = now.strftime("%Y-%m-%d")
    lines: list[str] = []
    lines.append(f"# PR Digest — charmed-hpc — {today}")
    lines.append("")

    if not all_prs:
        lines.append("_No open PRs across tracked repos. :tada:_")
        lines.append("")
        return "\n".join(lines)

    repo_count = len(repos_with_prs)
    total_prs = len(all_prs)
    reviewer_load = build_reviewer_load(
        [("", pr) for pr in all_prs]  # repo not used by build_reviewer_load
    )

    needs_attention: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    for pr in all_prs:
        bucket = bucket_pr(pr, now, thresholds)
        if bucket == "needs_attention":
            needs_attention.append(pr)
        elif bucket == "stale":
            stale.append(pr)
        else:
            active.append(pr)

    # Order each bucket: needs_attention by oldest first (most at risk),
    # stale by most recent activity first (so the freshest "stale" PRs
    # appear at the top — they're closer to being re-awakened), and
    # active by most recent activity.
    needs_attention.sort(key=lambda p: p["created_at"])
    stale.sort(key=lambda p: p["last_activity"], reverse=True)
    active.sort(key=lambda p: p["last_activity"], reverse=True)

    lines.append("## Org summary")
    lines.append(
        f"- {total_prs} open PR{'s' if total_prs != 1 else ''} across {repo_count} "
        f"repo{'s' if repo_count != 1 else ''}"
    )
    lines.append(
        f"- {len(needs_attention)} new but quiet (≤ "
        f"{int(thresholds['new_max_comments'])} comments, opened within "
        f"{int(thresholds['new_max_hours'])} business hours)"
    )
    lines.append(
        f"- {len(stale)} stale (no business-hour activity in "
        f"≥ {int(thresholds['stale_min_hours'])}h)"
    )
    lines.append(f"- {len(active)} active")
    if reviewer_load:
        load_str = ", ".join(f"@{u} ({n})" for u, n in reviewer_load[:5])
        lines.append(f"- Reviewer load (top): {load_str}")
    lines.append("")

    for title, group in (
        ("Needs attention", needs_attention),
        ("Active", active),
        ("Stale / dead", stale),
    ):
        section = build_pr_section(title, group, now)
        if section:
            lines.append(section)

    return "\n".join(lines)


def fetch_repo(
    repo_full: str, token: str
) -> tuple[str, list[dict[str, Any]]] | None:
    """Fetch all open PRs for a repo with enriched info. Returns None on total failure."""
    if "/" not in repo_full:
        print(f"  ! skipping malformed entry: {repo_full!r}", file=sys.stderr)
        return None
    owner, repo = repo_full.split("/", 1)
    try:
        raw_prs = list_open_prs(owner, repo, token)
    except urllib.error.HTTPError as e:
        print(f"  ! {repo_full}: HTTP {e.code} listing PRs — skipping", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  ! {repo_full}: network error ({e.reason}) — skipping", file=sys.stderr)
        return None

    enriched: list[dict[str, Any]] = []
    for pr in raw_prs:
        try:
            enriched.append(collect_pr_activity(pr, token))
        except urllib.error.HTTPError as e:
            print(
                f"  ! {repo_full}#{pr['number']}: HTTP {e.code} on detail — skipping this PR",
                file=sys.stderr,
            )
        except urllib.error.URLError as e:
            print(
                f"  ! {repo_full}#{pr['number']}: network error ({e.reason}) — skipping this PR",
                file=sys.stderr,
            )
    return (repo_full, enriched)


def post_to_mattermost(webhook_url: str, text: str) -> None:
    """POST the digest text to a Mattermost incoming webhook."""
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "pr-digest"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 300:
            raise RuntimeError(
                f"Mattermost webhook returned {resp.status}: {body[:300]}"
            )


def main() -> int:
    token = os.environ.get("GH_TOKEN")
    webhook_url = os.environ.get("MATTERMOST_WEBHOOK_URL")
    if not token:
        print("GH_TOKEN env var is required", file=sys.stderr)
        return 2
    if not webhook_url:
        print("MATTERMOST_WEBHOOK_URL env var is required", file=sys.stderr)
        return 2

    repos_path = Path(
        os.environ.get("REPOS_FILE", Path(__file__).parent.parent / "repos.yaml")
    )
    repos, thresholds = load_repos(repos_path)
    if not repos:
        print(f"No repos listed in {repos_path}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    print(f"Fetching PRs from {len(repos)} repos…", file=sys.stderr)
    results: list[tuple[str, list[dict[str, Any]]]] = []
    for r in repos:
        print(f"  - {r}", file=sys.stderr)
        result = fetch_repo(r, token)
        if result is not None and result[1]:
            results.append(result)

    digest = build_digest(results, now, thresholds)

    if os.environ.get("DRY_RUN") == "1":
        print("---- DRY RUN: not posting to Mattermost ----", file=sys.stderr)
        print(digest)
        return 0

    print("Posting to Mattermost…", file=sys.stderr)
    post_to_mattermost(webhook_url, digest)
    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
