"""Weekly PR digest for the charmed-hpc org, posted to a Matrix room.

Combines the same open-PR digest (Needs attention / Active / Stale)
as the daily Mattermost post with a "Merged this week" recap that
lists PRs merged in the last 7 days, with title, body excerpt, labels,
and diff stats.

Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pr_digest import (
    build_digest,
    fetch_repo,
    load_repos,
    list_merged_prs_since,
)

# Body excerpts in the merged-this-week table are clipped to this many
# characters. Long enough to convey the change, short enough to keep
# the table readable.
BODY_EXCERPT_CHARS = 200
WEEK_DAYS = 7


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
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 300:
            raise RuntimeError(
                f"Matrix API returned {resp.status}: {body[:300]}"
            )


def _excerpt(body: str | None) -> str:
    """Return a short excerpt of the PR body, with newlines flattened."""
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
    return ", ".join(
        f"`{l['name']}`" for l in labels if l.get("name")
    )


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
    body_excerpt = _excerpt(pr.get("body"))
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
    """Fetch merged PRs for a repo since the cutoff. Returns None on total failure."""
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
    return (repo_full, merged)


def main() -> int:
    token = os.environ.get("GH_TOKEN")
    homeserver = os.environ.get("MATRIX_HOMESERVER")
    access_token = os.environ.get("MATRIX_ACCESS_TOKEN")
    room_id = os.environ.get("MATRIX_ROOM_ID")
    missing = [
        name
        for name, val in (
            ("GH_TOKEN", token),
            ("MATRIX_HOMESERVER", homeserver),
            ("MATRIX_ACCESS_TOKEN", access_token),
            ("MATRIX_ROOM_ID", room_id),
        )
        if not val
    ]
    if missing:
        print(
            f"Missing required env vars: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    repos_path = Path(
        os.environ.get("REPOS_FILE", Path(__file__).parent.parent / "repos.yaml")
    )
    repos, thresholds = load_repos(repos_path)
    if not repos:
        print(f"No repos listed in {repos_path}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=WEEK_DAYS)

    # Open PRs (full digest).
    print(f"Fetching open PRs from {len(repos)} repos…", file=sys.stderr)
    open_results: list[tuple[str, list[dict[str, Any]]]] = []
    for r in repos:
        print(f"  - {r} (open)", file=sys.stderr)
        result = fetch_repo(r, token)
        if result is not None and result[1]:
            open_results.append(result)

    # Merged PRs in the last 7 days.
    print(
        f"Fetching merged PRs since {week_ago.isoformat()}…", file=sys.stderr
    )
    merged_results: list[tuple[str, list[dict[str, Any]]]] = []
    for r in repos:
        print(f"  - {r} (merged)", file=sys.stderr)
        result = fetch_merged_for_repo(r, token, week_ago)
        if result is not None and result[1]:
            merged_results.append(result)

    open_digest = build_digest(open_results, now, thresholds)
    merged_section = build_merged_section(merged_results)
    digest = open_digest.rstrip() + "\n" + (merged_section or "")

    if os.environ.get("DRY_RUN") == "1":
        print("---- DRY RUN: not posting to Matrix ----", file=sys.stderr)
        print(digest)
        return 0

    print("Posting to Matrix…", file=sys.stderr)
    post_to_matrix(homeserver, access_token, room_id, digest)  # type: ignore[arg-type]
    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
