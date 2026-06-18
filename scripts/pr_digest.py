"""PR digest for the charmed-hpc org.

One script, two post targets (Mattermost and Matrix), selected by
the `POST_TARGET` env var. Both modes read `repos.yaml`, query the
GitHub API for open PRs in each repo, compute reviewer load and
business-hour staleness, and render a single combined markdown
digest. When `INCLUDE_MERGED=1`, the digest additionally fetches PRs
merged in the `MERGED_WINDOW` (default 7) most recent days and renders
them as a "Merged this week" section above the open-PR buckets — this
is independent of the post target.

Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from business_hours import business_hours_between


GITHUB_API = "https://api.github.com"
STALE_THRESHOLD_HOURS = 24.0
HTTP_TIMEOUT = 30
BODY_EXCERPT_CHARS = 200  # merged-PR body excerpt length

# Truthy values accepted for boolean env vars (DRY_RUN, INCLUDE_*).
# Both the unquoted YAML form (1) and the quoted form ('1') end up as
# the Python string "1", but a reader who writes the value differently
# should still get the expected behavior.
_TRUTHY = frozenset({"1", "true", "True", "yes", "Yes", "on", "On"})


def _truthy(name: str, default: str = "") -> bool:
    """Read an env var and return True iff it parses as truthy.

    `default` is the value to use when the env var is unset. An unset
    or empty value returns False. The comparison is case-insensitive
    against the `_TRUTHY` set.
    """
    raw = os.environ.get(name, default)
    return str(raw).strip() in _TRUTHY


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
        rendered = []
        for u, n in pr["top_commenters_all"]:
            # Render every commenter (including the PR author) as
            # `@login (N)` with a space, so Mattermost autolinks the
            # @name to a real mention.
            rendered.append(f"@{u} ({n})")
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
    include_stale: bool = True,
    include_needs_attention: bool = True,
    include_merged: bool = False,
    token: str | None = None,
    merged_window_days: int = 7,
) -> str:
    """Build the full markdown digest string, bucketed by activity tier.

    `include_stale` and `include_needs_attention` let callers suppress
    one or both open-PR buckets. The Org summary counts always reflect
    the *full* set, not the displayed subset, so the totals don't lie
    about what's in the org. The suppressed bucket is just omitted from
    the rendered sections.

    `include_merged` is independent of the post target: when True, the
    digest fetches PRs merged in the last `merged_window_days` days and
    inserts a "Merged this week" section between the Org summary and
    the open-PR buckets, plus a "N merged this week" line in the Org
    summary. The fetch and render happen here so the digest-building
    logic is decoupled from which backend the post target uses; main()
    just decides whether to include merged PRs.

    `token` is required when `include_merged` is True. If it's
    missing the fetch step is skipped and the digest renders without
    the merged section (a warning is printed to stderr).
    """
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

    # Fetch merged PRs for the recap section when requested. The fetch
    # happens here (rather than in main()) so the digest-building
    # logic is independent of the post target: the only thing main()
    # decides is whether the digest should include the merged recap.
    merged_count = 0
    merged_section = ""
    if include_merged:
        if not token:
            print(
                "  ! INCLUDE_MERGED=1 but no GH_TOKEN available — "
                "skipping merged section",
                file=sys.stderr,
            )
        else:
            week_ago = now - timedelta(days=merged_window_days)
            merged_results: list[tuple[str, list[dict[str, Any]]]] = []
            for repo_full, _prs in repos_with_prs:
                result = fetch_merged_for_repo(repo_full, token, week_ago)
                if result is not None and result[1]:
                    merged_results.append(result)
            merged_count = sum(len(prs) for _, prs in merged_results)
            merged_section = build_merged_section(merged_results)

    lines.append("## Org summary")
    if include_merged:
        lines.append(f"- {merged_count} merged this week")
    lines.append(
        f"- {total_prs} open PR{'s' if total_prs != 1 else ''} across {repo_count} "
        f"repo{'s' if repo_count != 1 else ''}"
    )
    if include_needs_attention:
        lines.append(
            f"- {len(needs_attention)} new but quiet (≤ "
            f"{int(thresholds['new_max_comments'])} comments, opened within "
            f"{int(thresholds['new_max_hours'])} business hours)"
        )
    if include_stale:
        lines.append(
            f"- {len(stale)} stale (no business-hour activity in "
            f"≥ {int(thresholds['stale_min_hours'])}h)"
        )
    lines.append(f"- {len(active)} active")
    if reviewer_load:
        load_str = ", ".join(f"@{u} ({n})" for u, n in reviewer_load[:5])
        lines.append(f"- Reviewer load (top): {load_str}")
    lines.append("")

    # Merged-this-week sits between the Org summary and the open-PR
    # buckets when INCLUDE_MERGED is set, so the merged recap leads
    # the rendered output. The empty-string case (no merged PRs)
    # cleanly skips the section without losing the Org summary line.
    if merged_section:
        lines.append(merged_section.rstrip())
        lines.append("")

    rendered_buckets: list[tuple[str, list[dict[str, Any]]]] = []
    if include_needs_attention:
        rendered_buckets.append(("Needs attention", needs_attention))
    rendered_buckets.append(("Active", active))
    if include_stale:
        rendered_buckets.append(("Stale / dead", stale))

    for title, group in rendered_buckets:
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


def post_to_matrix(
    homeserver: str, access_token: str, room_id: str, text: str
) -> None:
    """POST `text` as a plain m.text message to a Matrix room.

    Uses the user access token (no login on the hot path). The body
    is sent as both `body` (plain markdown) and `formatted_body` so
    clients that support HTML rendering can show the formatted
    version, while others fall back to the plain text.
    """
    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/rooms/{room_id}/send/m.room.message"
    txn_id = os.urandom(8).hex()
    url = f"{url}/{txn_id}"
    payload = {
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        # We don't have a real markdown→HTML renderer, so we send the
        # same markdown string in both fields. Clients that render
        # `formatted_body` will show it as plain text; the `body`
        # field is the authoritative plain-text view.
        "formatted_body": text,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "pr-digest",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 300:
            raise RuntimeError(
                f"Matrix API returned {resp.status}: {body[:300]}"
            )


# --------------------------------------------------------------------------
# Merged-this-week helpers (Matrix mode only)
# --------------------------------------------------------------------------


def _merged_excerpt(body: str | None) -> str:
    """Return a short excerpt of a merged PR body, with newlines flattened."""
    if not body:
        return ""
    flat = " ".join(body.split())
    if len(flat) <= BODY_EXCERPT_CHARS:
        return flat
    return flat[: BODY_EXCERPT_CHARS - 1].rstrip() + "…"


def _labels_cell(labels: list[dict[str, Any]]) -> str:
    """Render labels as a comma-separated list. Empty list → empty string."""
    if not labels:
        return ""
    return ", ".join(f"`{l['name']}`" for l in labels if l.get("name"))


def _esc(s: str) -> str:
    """Escape pipe characters in a markdown table cell."""
    return s.replace("|", "\\|")


def build_merged_row(
    pr: dict[str, Any], repo_short: str, include_labels: bool
) -> str:
    """Render a single merged PR as a markdown table row.

    The Labels column is only included if `include_labels` is True;
    when False the rendered row is 4-cell to match the 4-cell header.
    """
    number = pr["number"]
    url = pr["html_url"]
    title = pr["title"]
    if len(title) > 60:
        title = title[:60] + "…"
    author = pr["user"]["login"]
    body_excerpt = _merged_excerpt(pr.get("body"))
    # additions/deletions are populated by fetch_merged_for_repo via
    # a follow-up detail call (the list endpoint omits them). The
    # `or 0` fallback covers the rare case where a single detail
    # call failed and the PR is still rendered with empty stats.
    additions = pr.get("additions", 0) or 0
    deletions = pr.get("deletions", 0) or 0
    diff_cell = f"+{additions} / -{deletions}"

    cells: list[str] = [
        f"[{repo_short}#{number}]({url}) {_esc(title)}",
        f"@{_esc(author)}",
        _esc(body_excerpt) or "_(no description)_",
    ]
    if include_labels:
        labels_cell = _labels_cell(pr.get("labels") or [])
        cells.append(_esc(labels_cell))
    cells.append(diff_cell)
    return "| " + " | ".join(cells) + " |"


def build_merged_section(
    repos_with_merged: list[tuple[str, list[dict[str, Any]]]],
) -> str:
    """Render the full 'Merged this week' markdown block.

    Returns an empty string if there are no merged PRs across all
    repos, so the digest can skip the heading entirely. The Labels
    column is only included for a repo if at least one of its PRs
    has a label.
    """
    total = sum(len(prs) for _, prs in repos_with_merged)
    if total == 0:
        return ""

    lines: list[str] = [f"## Merged this week ({total})", ""]

    for repo_full, prs in repos_with_merged:
        if not prs:
            continue
        repo_short = repo_full.split("/", 1)[-1]
        any_labels = any(pr.get("labels") for pr in prs)
        lines.append(f"### {repo_full} ({len(prs)})")
        lines.append("")

        if any_labels:
            lines.append("| PR | Author | Description | Labels | Diff |")
            lines.append("|---|---|---|---|---|")
        else:
            lines.append("| PR | Author | Description | Diff |")
            lines.append("|---|---|---|---|")

        for pr in prs:
            lines.append(build_merged_row(pr, repo_short, any_labels))
        lines.append("")

    return "\n".join(lines)


def fetch_merged_for_repo(
    repo_full: str, token: str, since: datetime
) -> tuple[str, list[dict[str, Any]]] | None:
    """Fetch merged PRs for a repo since the cutoff. Returns None on total failure.

    The GitHub pulls list endpoint (state=closed) does not populate
    `additions` / `deletions` — those fields only appear on the
    detail endpoint. So for each merged PR we make a follow-up call
    to fetch full PR detail and merge those diff stats into the
    record. If the detail call fails for one PR we log a warning and
    keep the PR with empty diff stats, rather than dropping it.
    """
    if "/" not in repo_full:
        print(f"  ! skipping malformed entry: {repo_full!r}", file=sys.stderr)
        return None
    owner, repo = repo_full.split("/", 1)
    try:
        merged = list_merged_prs_since(owner, repo, token, since)
    except urllib.error.HTTPError as e:
        print(
            f"  ! {repo_full}: HTTP {e.code} listing merged PRs — skipping",
            file=sys.stderr,
        )
        return None
    except urllib.error.URLError as e:
        print(
            f"  ! {repo_full}: network error ({e.reason}) — skipping",
            file=sys.stderr,
        )
        return None

    for pr in merged:
        try:
            detail = get_pr_detail(owner, repo, pr["number"], token)
        except urllib.error.HTTPError as e:
            print(
                f"  ! {repo_full}#{pr['number']}: HTTP {e.code} on detail "
                f"— diff stats will be empty",
                file=sys.stderr,
            )
            continue
        except urllib.error.URLError as e:
            print(
                f"  ! {repo_full}#{pr['number']}: network error ({e.reason}) "
                f"— diff stats will be empty",
                file=sys.stderr,
            )
            continue
        if not detail:
            continue
        # Overlay detail fields onto the list record. We intentionally
        # don't replace the whole record — the list response has
        # `merged_at`, the sort key, and we want to preserve it.
        for k in ("additions", "deletions"):
            if k in detail:
                pr[k] = detail[k]

    return (repo_full, merged)


def main() -> int:
    """Entry point. Dispatches on `POST_TARGET` env var.

    `POST_TARGET=mattermost` (default) — render the open-PR digest
    and POST to the Mattermost webhook. All three open buckets
    (Needs attention, Active, Stale/dead) render by default.

    `POST_TARGET=matrix` — render the open-PR digest (Active only
    by default) and POST to a Matrix room using a user access token.

    Common env vars:
      GH_TOKEN         (required) fine-grained GitHub PAT
      POST_TARGET      mattermost | matrix (default: mattermost)
      DRY_RUN          truthy = log digest instead of posting
                       (accepts 1, '1', 'true', 'yes', 'on')
      INCLUDE_STALE    falsy = hide Stale/dead bucket + Org line
                       (default: shown)
      INCLUDE_NEEDS_ATTENTION  falsy = hide Needs attention + line
                       (default: shown)
      INCLUDE_MERGED   truthy = add a "Merged this week" section
                       + a "N merged this week" line in the Org
                       summary. Independent of POST_TARGET —
                       default off; set to 1 in any workflow that
                       wants the weekly recap.
      REPOS_FILE       path to repos.yaml (default: ./repos.yaml)
      MERGED_WINDOW    days of merged-PR history (only used when
                       INCLUDE_MERGED=1, default 7)

    Mattermost target also requires:
      MATTERMOST_WEBHOOK_URL

    Matrix target also requires:
      MATRIX_HOMESERVER, MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID
    """
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("GH_TOKEN env var is required", file=sys.stderr)
        return 2

    target = os.environ.get("POST_TARGET", "mattermost").lower()
    if target not in ("mattermost", "matrix"):
        print(
            f"POST_TARGET must be 'mattermost' or 'matrix' (got {target!r})",
            file=sys.stderr,
        )
        return 2

    include_stale = _truthy("INCLUDE_STALE", "1")
    include_needs_attention = _truthy("INCLUDE_NEEDS_ATTENTION", "1")
    include_merged = _truthy("INCLUDE_MERGED")

    repos_path = Path(
        os.environ.get("REPOS_FILE", Path(__file__).parent.parent / "repos.yaml")
    )
    repos, thresholds = load_repos(repos_path)
    if not repos:
        print(f"No repos listed in {repos_path}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    print(f"Fetching open PRs from {len(repos)} repos…", file=sys.stderr)
    open_results: list[tuple[str, list[dict[str, Any]]]] = []
    for r in repos:
        print(f"  - {r} (open)", file=sys.stderr)
        result = fetch_repo(r, token)
        if result is not None and result[1]:
            open_results.append(result)

    # Merged-PR fetch is driven by INCLUDE_MERGED, not by target. The
    # fetch and render happen inside build_digest so the digest shape
    # is independent of where it gets posted. MERGED_WINDOW still
    # controls how many days back to look.
    if include_merged:
        try:
            merged_window_days = int(os.environ.get("MERGED_WINDOW", "7"))
        except ValueError:
            print(
                "MERGED_WINDOW must be an integer (number of days)",
                file=sys.stderr,
            )
            return 2
    else:
        merged_window_days = 7  # unused; only read when INCLUDE_MERGED=1

    digest = build_digest(
        open_results,
        now,
        thresholds,
        include_stale=include_stale,
        include_needs_attention=include_needs_attention,
        include_merged=include_merged,
        token=token if include_merged else None,
        merged_window_days=merged_window_days,
    )

    if _truthy("DRY_RUN"):
        print(
            f"---- DRY RUN: not posting to {target} ----",
            file=sys.stderr,
        )
        print(digest)
        return 0

    if target == "matrix":
        homeserver = os.environ.get("MATRIX_HOMESERVER")
        access_token = os.environ.get("MATRIX_ACCESS_TOKEN")
        room_id = os.environ.get("MATRIX_ROOM_ID")
        missing = [
            n for n, v in (
                ("MATRIX_HOMESERVER", homeserver),
                ("MATRIX_ACCESS_TOKEN", access_token),
                ("MATRIX_ROOM_ID", room_id),
            ) if not v
        ]
        if missing:
            print(
                f"Missing required env vars for POST_TARGET=matrix: "
                f"{', '.join(missing)}",
                file=sys.stderr,
            )
            return 2
        print("Posting to Matrix…", file=sys.stderr)
        post_to_matrix(homeserver, access_token, room_id, digest)  # type: ignore[arg-type]
    else:
        webhook_url = os.environ.get("MATTERMOST_WEBHOOK_URL")
        if not webhook_url:
            print("MATTERMOST_WEBHOOK_URL env var is required", file=sys.stderr)
            return 2
        print("Posting to Mattermost…", file=sys.stderr)
        post_to_mattermost(webhook_url, digest)

    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
