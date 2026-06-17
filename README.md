Daily PR digest for the `charmed-hpc` org, posted to a Mattermost
channel and a weekly recap posted to a Matrix room. The daily digest
is bucketed by activity (Needs attention, Active, Stale / dead) with
comment and reviewer activity, and the weekly recap adds a
"Merged this week" section. Made with AI agents (Minimax primarily).

## What it does

Two workflows run from this repo:

**Daily (Mattermost)** — `.github/workflows/pr-digest.yml`:

1. Reads the list of repos from `repos.yaml`
2. Queries the GitHub API for open PRs in each
3. Buckets PRs into Needs attention, Active, and Stale / dead using
   business-hour thresholds
4. Renders a single combined markdown digest
5. Posts it to a Mattermost incoming webhook

**Weekly (Matrix)** — `.github/workflows/pr-digest-matrix.yml`:

1. Builds the same open-PR digest as the daily run
2. Fetches PRs merged in the last 7 days, with body excerpts, labels,
   and diff stats
3. Renders both as a single combined markdown digest
4. Posts it to a Matrix room using a user access token

No long-lived service. No external scheduler. Runs in a GitHub-hosted
runner that's destroyed after each run.

## Files in this repo

```
.github/workflows/pr-digest.yml        # Daily schedule + Mattermost run
.github/workflows/pr-digest-matrix.yml # Weekly schedule + Matrix run
scripts/pr_digest.py             # Daily: fetches, renders, posts to Mattermost
scripts/pr_digest_matrix.py      # Weekly: adds merged-this-week + posts to Matrix
scripts/business_hours.py        # Weekday-aware delta math
repos.yaml                       # List of repos to scan + activity thresholds
README.md                        # This file
examples/caller-workflow.yml     # Reference for triggering from elsewhere
```

## Editing the repo list

Open `repos.yaml` and add or remove entries:

```yaml
repos:
  - charmed-hpc/slurmctld
  - charmed-hpc/slurmd
  - charmed-hpc/new-repo    # added
  # - charmed-hpc/old-repo  # disabled, will be skipped
```

Commit the change. The next scheduled run uses the new list. **You
also need to update the PAT scope** to include any newly added repos.

---

## Setup

### 1. Create this repo

Create a **private** repo under your user account. The exact name
doesn't matter, but `pr-digest` is the convention used here. Push the
contents of this directory.

### 2. Get a Mattermost incoming webhook

In Mattermost:

1. Go to the target channel (create one if needed, e.g. `#pr-digest`)
2. Channel name → Integrations → Incoming Webhooks → Add Incoming Webhook
3. Give it a display name (`PR Digest`) and an icon if you like
4. Copy the webhook URL — it's the only credential you'll get

If incoming webhooks are disabled at the system level, ask your
Mattermost admin to enable them under
**System Console → Integrations → Integration Management**.

### 3. Create a fine-grained GitHub PAT

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

### 4. Store the secrets

In the `pr-digest` repo: Settings → Secrets and variables → Actions →
New repository secret. Add the Mattermost pair (the Matrix pair is
optional — skip section 5 if you don't want the weekly post):

| Name | Value |
|---|---|
| `GH_TOKEN` | The fine-grained PAT from step 3 |
| `MATTERMOST_WEBHOOK_URL` | The webhook URL from step 2 |

### 5. (Optional) Set up Matrix for the weekly digest

The weekly recap posts to a Matrix room. You need a bot account and
a room it's joined.

**5a. Create a bot account.** Register a regular Matrix account on
your homeserver, e.g. `@pr-digest:matrix.org`. Use Element Web (or
any client) to sign up, set a password, and complete any email or
captcha verification. Treat the password like a normal account
password — rotate it the same way.

**5b. Join the target room.** From the bot account, join the room
you want digests in (e.g. `#pr-digest:matrix.org`). Bots can only
post to rooms they've joined. If the room is end-to-end-encrypted,
use an unencrypted room instead — the simple HTTP bot can't post
encrypted messages.

**5c. Get an access token.** From any machine with `curl`, log the
bot in once:

```bash
curl -X POST https://matrix.org/_matrix/client/v3/login \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "m.login.password",
    "user": "pr-digest",
    "password": "..."
  }'
```

The response includes `access_token` and `user_id`. Copy the
`access_token` — that's the only secret you need to store. The
homeserver URL (`https://matrix.org` in the example) is also a
secret, just a public one.

If the homeserver is not `matrix.org`, replace the URL in the
`curl` command with your homeserver's client-API base.

**5d. Find the room ID.** In Element Web, open the room →
Room Settings → Advanced → "Internal room ID". It looks like
`!abc123:matrix.org`. Or via the API:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://matrix.org/_matrix/client/v3/joined_rooms
```

The response lists room IDs the bot is a member of.

**5e. Add three more secrets:**

| Name | Value |
|---|---|
| `MATRIX_HOMESERVER` | The homeserver URL, e.g. `https://matrix.org` |
| `MATRIX_ACCESS_TOKEN` | The `access_token` from 5c |
| `MATRIX_ROOM_ID` | The room ID from 5d |

**5f. Test.** Trigger the "PR Digest — Matrix (weekly)" workflow
from the Actions tab. Within ~30 seconds the weekly recap should
appear in the Matrix room. If it doesn't, check the Actions log —
common causes are 401 (token wrong or expired) or 403 (bot not in
the room).

### 6. Test the daily run

Go to the Actions tab in the `pr-digest` repo, select "PR Digest",
click "Run workflow". Within ~30 seconds you should see the digest
message appear in the Mattermost channel.

### 7. Schedule

Edit the `cron:` line in the workflow file(s) to set your preferred
schedule. Cron is in UTC.

- `.github/workflows/pr-digest.yml` — daily Mattermost post
- `.github/workflows/pr-digest-matrix.yml` — weekly Matrix post

## Migrating the project to a new org

If the `charmed-hpc` org moves or gets renamed:

1. Issue a new fine-grained PAT scoped to the new repo names in the
   new org
2. Update `repos.yaml` with the new `<owner>/<repo>` entries
3. Update the `GH_TOKEN` secret with the new PAT value
4. No other change needed — script, workflow, webhook, schedule all
   stay the same

There may be a one-day gap between migration and the first successful
digest on the new org. To avoid it, create the new PAT in advance and
swap the secret in the same commit as the `repos.yaml` change.

## Migrating this digest infra to a different repo

1. GitHub transfer: Settings → Danger Zone → Transfer ownership
   (new owner accepts)
2. **OR** clone-push: `git clone <old-url>`, create new repo, push
3. **Secrets do not transfer.** Re-add `GH_TOKEN` and
   `MATTERMOST_WEBHOOK_URL` in the new repo's settings. The values
   themselves can be reused.

The workflow schedule, Actions history, and git history all move with
the repo.

## Limitations and known issues

- **PAT expiration**: when the fine-grained PAT expires, the script
  starts failing silently (no digest appears, but the Action run
  shows a 401). Set a calendar reminder to rotate every 90 days.
- **GitHub Actions "approximate" timing**: scheduled runs can be
  delayed up to ~30 minutes under load. For a daily digest, this is
  fine.
- **60-day inactivity pause**: if this repo has no activity for 60+
  days, GitHub may pause its scheduled workflows. This repo will see
  activity from you editing it, so it's unlikely — but if you abandon
  the repo, expect the digest to stop.
- **Rate limits**: the script makes 1 + (3 × open PRs) API calls per
  repo. With the 5,000/hr authenticated limit, this is not a concern
  at expected scale.
- **No retry logic**: a single repo failing (404, 403, 5xx) is logged
  and skipped; the rest of the digest still posts. A complete GitHub
  outage will result in no digest that day.

## Manual run / debugging

The workflow has a `workflow_dispatch` trigger. From the Actions tab:

- **Run workflow** to trigger an immediate run
- Add `DRY_RUN=1` as a temporary env var in the workflow to print
  the digest to the Actions log instead of posting to Mattermost
  (useful when iterating on the format)

## Security notes

- The Mattermost webhook URL is a credential. Treat it like a password.
  Anyone with the URL can post to that channel as the webhook.
- The fine-grained PAT is scoped to read-only on the specific repos
  you select. It does not grant write access to anything.
- Both are stored as GitHub Actions encrypted secrets, which are not
  exposed to forks or to PRs from forks.



