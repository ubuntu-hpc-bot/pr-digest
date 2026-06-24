PR Digest summarizes pull requests for the `charmed-hpc` org and posts them to one of two configurable **targets**: Mattermost or Matrix. Each target has its own workflow, its own schedule (cron, in UTC), and its own secrets. Digests are bucketed by activity (Merged this week, Needs attention, Active, Stale / dead) and include comment and reviewer activity.

*Made with AI agents (Minimax primarily).*

## What it does

Two workflows run from this repo by default, both invoking the same script
— [scripts/pr_digest.py](scripts/pr_digest.py) — and selecting
the post backend via the `POST_TARGET` env var:

**Mattermost** — `.github/workflows/pr-digest-mattermost.yml`:

1. Reads the list of repos from `repos.yaml`
2. Queries the GitHub API for open PRs in each
3. Buckets PRs into Needs attention, Active, and Stale / dead using
   business-hour thresholds
4. Renders a single combined markdown digest
5. Posts it to a Mattermost incoming webhook

**Matrix** — `.github/workflows/pr-digest-matrix.yml`:

1. Builds the same open-PR digest as the Mattermost run, plus a
   "Merged this week" section (because it sets `INCLUDE_MERGED=1`)
2. Fetches PRs merged in the last `MERGED_WINDOW` days (default 7),
   with body excerpts, labels, and diff stats
3. Renders both as a single combined markdown digest
4. Posts it to a Matrix room using a user access token

The "Merged this week" section is opt-in via the `INCLUDE_MERGED`
env var, independent of `POST_TARGET`. The Mattermost workflow
leaves it off (daily check-ins focus on what's open); the Matrix
workflow turns it on for the weekly recap. Either target can enable
or disable it without code changes — set/unset `INCLUDE_MERGED` in
the workflow's `env:` block.

The two targets are independent. Enable either, both, or neither —
the only thing they share is the same `repos.yaml` and the same
GitHub PAT. In this repo, the Mattermost workflow runs weekdays at 09:00 UTC and the Matrix workflow runs Mondays at 09:00 UTC. The cron expressions in each workflow file can be edited freely.

There is no long-lived service or external scheduler. Each run executes on a GitHub-hosted runner that is destroyed when the run completes.

## Files in this repo

```
.github/workflows/pr-digest-mattermost.yml # Mattermost target: schedule + run
.github/workflows/pr-digest-matrix.yml     # Matrix target: schedule + run
scripts/pr_digest.py             # Both targets: fetch, render, post
scripts/business_hours.py        # Weekday-aware delta math
repos.yaml                       # List of repos to scan + activity thresholds
README.md                        # This file
examples/caller-workflow.yml     # Reference for triggering from elsewhere
```

## Setup to duplicate for your own org

### 1. Create this repo

Create a **private** repo under your user account (the name `pr-digest` is the convention used here) and push the contents of this directory.

### 2. Create a fine-grained GitHub PAT

At <https://github.com/settings/personal-access-tokens/new>:

- **Resource owner**: `charmed-hpc` (the org that owns the repos)
- **Repository access**: Only select repositories
  - Select every repo listed in `repos.yaml`
  - When you add/remove a repo from `repos.yaml`, update the PAT scope
    to match
- **Permissions**:
  - Repository → Metadata: **Read-only**
  - Repository → Pull requests: **Read-only**
  - Repository → Issues: **Read-only**

Set an expiration (90 days is typical). Put a calendar reminder to
rotate it before expiry.

### 3. Store the shared secret

In the `pr-digest` repo: Settings → Secrets and variables → Actions →
New repository secret.

| Name | Value |
|---|---|
| `GH_TOKEN` | The fine-grained PAT from step 2 |

### 4. Setup Mattermost Target

**4a. Create the Mattermost incoming webhook**

In Mattermost:

1. Go to the target channel (create one if needed, e.g. `#pr-digest`)
2. Channel name → Integrations → Incoming Webhooks → Add Incoming Webhook
3. Give it a display name (`PR Digest`) and an icon if you like
4. Copy the webhook URL — it's the only credential you'll get

If incoming webhooks are disabled at the system level, ask your
Mattermost admin to enable them under
**System Console → Integrations → Integration Management**.

**4b. Add the secret:**

| Name | Value |
|---|---|
| `MATTERMOST_WEBHOOK_URL` | The webhook URL from step 4a |

**4c. The default Mattermost workflow** runs weekdays at 09:00 UTC.
Edit the `cron:` line in `.github/workflows/pr-digest-mattermost.yml`
to change the schedule or frequency.

### 5. Set up the Matrix target

**5a. Create a bot account.** Register a regular Matrix account on your homeserver (e.g. `@pr-digest:matrix.org`). Use Element Web or any other client to sign up, set a password, and complete any email or captcha verification. Rotate the password on the same schedule as any other account credential.

**5b. Join the target room.** From the bot account, join the room
you want digests in (e.g. `#pr-digest:matrix.org`). Bots can only
post to rooms they've joined. If the room is end-to-end-encrypted,
use an unencrypted room instead — the simple HTTP bot can't post
encrypted messages.

**5c. Get an access token.** In Element Web, log in as the bot
account → click the avatar → **All settings** → **Help & About** →
the access token is shown there. Copy it. This works for any
account type, including SSO-only accounts (GitHub, Apple, etc.)
that don't have a password set.

**Security best practice**: Matrix access tokens issued via Element
do not automatically expire. Set a calendar reminder to rotate the
token every 90 days (same schedule as the GitHub PAT). To rotate:
log out and back in to the bot account to invalidate the old token,
get the new token from Settings, and update the `MATRIX_ACCESS_TOKEN`
secret in the repository.

The homeserver URL (`https://matrix.org` in the example) is also stored as a secret, though it is public information. **Note**: The homeserver must use HTTPS — HTTP URLs are rejected by the script for security reasons.

**5d. Find the room ID.** `MATRIX_ROOM_ID` needs the room's full ID
(starting with `!`), not its alias (starting with `#`). Element Web
accepts both in the URL bar, so the address bar alone isn't enough
to tell which one you have. **Note**: The script validates that the
room ID starts with `!` and will reject aliases for security and
correctness.

*From the address bar (when Element shows the ID):*

```
https://app.element.io/#/room/!abc123xyz:matrix.org
```

If the URL starts with `!`, copy that part as-is. The "Internal
room ID" shown in Room Settings → Advanced is a shortened display
version and won't work as `MATRIX_ROOM_ID`.

*From the address bar (when Element shows an alias):*

```
https://app.element.io/#/room/#hpc-newswire:ubuntu.com
```

If the URL starts with `#`, that's an alias — resolve it to an ID
via the directory endpoint. URL-encode the `#` as `%23` and the `:`
as `%3A`:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://matrix.org/_matrix/client/v3/directory/room/%23hpc-newswire%3Aubuntu.com"
```

The response includes a `room_id` field starting with `!` — that's
your `MATRIX_ROOM_ID`. If the alias is on a different homeserver
than the one in `MATRIX_HOMESERVER`, replace `matrix.org` in the
URL with the homeserver that owns the alias.

*From the API (rooms the bot has already joined):*

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://matrix.org/_matrix/client/v3/joined_rooms
```

The response is a bare list of room IDs with no names, so this only
helps if the bot is in exactly one room. For multiple rooms, use
the directory endpoint above with the alias you want.

**5e. Add three secrets:**

| Name | Value |
|---|---|
| `MATRIX_HOMESERVER` | The homeserver URL, e.g. `https://matrix.org` |
| `MATRIX_ACCESS_TOKEN` | The `access_token` from 5c |
| `MATRIX_ROOM_ID` | The room ID from 5d |

**5f. The default Matrix workflow** runs Mondays at 09:00 UTC. Edit
the `cron:` line in `.github/workflows/pr-digest-matrix.yml` to
change the schedule or frequency.

### 6. Test the targets

Each workflow has a `workflow_dispatch` trigger. From the Actions
tab in the `pr-digest` repo:

- **Mattermost target**: select "PR Digest — Mattermost (daily)",
  click "Run workflow". Within ~30 seconds the digest should appear
  in the Mattermost channel.
- **Matrix target**: select "PR Digest — Matrix (weekly)", click
  "Run workflow". Within ~30 seconds the digest should appear in
  the Matrix room.

To preview the rendered output without actually posting, set
`DRY_RUN: '1'` in the workflow's `env:` block (temporarily — revert
when you're done iterating).

### 7. Schedule

Each target has its own `cron:` line, in UTC, in its own workflow
file. Edit either to change the schedule or frequency.

- `.github/workflows/pr-digest-mattermost.yml` — Mattermost target
- `.github/workflows/pr-digest-matrix.yml` — Matrix target

## Limitations and known issues

- **PAT expiration**: when the fine-grained PAT expires, the script
  starts failing silently (no digest appears, but the Action run
  shows a 401). Set a calendar reminder to rotate every 90 days.
- **Matrix token expiration**: Matrix access tokens obtained from
  Element do not expire automatically. Set a calendar reminder to
  rotate every 90 days (same as the GitHub PAT).
- **GitHub Actions "approximate" timing**: scheduled runs can be
  delayed up to ~30 minutes under load. For any of the digests,
  this is fine.
- **60-day inactivity pause**: if this repo has no activity for 60+
  days, GitHub may pause its scheduled workflows. This repo will see
  activity from you editing it, so it's unlikely — but if you abandon
  the repo, expect the digest to stop.
- **No retry logic**: a single repo failing (404, 403, 5xx) is logged
  and skipped; the rest of the digest still posts. A complete GitHub
  outage will result in no digest that day.

## Security notes

- The Mattermost webhook URL is a credential. Treat it like a password.
  Anyone with the URL can post to that channel as the webhook.
- The Matrix access token has full privileges of the bot account. Treat
  it like a password and rotate it regularly (recommendation: every 90
  days, same as the GitHub PAT).
- The fine-grained PAT is scoped to read-only on the specific repos
  you select. It does not grant write access to anything.
- All credentials are stored as GitHub Actions encrypted secrets, which
  are not exposed to forks or to PRs from forks.
- **TLS/SSL verification**: The script uses Python's `urllib` with
  default settings, which means:
  - All HTTPS connections verify certificates using the system
    certificate store
  - Certificate verification cannot be disabled
  - Matrix homeserver URLs must use HTTPS (HTTP is rejected)
  - Mattermost webhook URLs should use HTTPS (strongly recommended)



