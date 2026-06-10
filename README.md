# pr-digest

Daily PR digest for the `charmed-hpc` org, posted to a Mattermost channel.
One combined message per day, grouped by repo, with reviewer load and
24-business-hour staleness flags.

## What it does

Once a day (default 09:00 UTC, weekdays), this workflow:

1. Reads the list of repos from `repos.yaml`
2. Queries the GitHub API for open PRs in each
3. Computes reviewer load and business-hour staleness
4. Renders a single combined markdown digest
5. Posts it to a Mattermost incoming webhook

No long-lived service. No external scheduler. Runs in a GitHub-hosted
runner that's destroyed after each run.

## One-time setup

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

Set an expiration (90 days is typical). Put a calendar reminder to
rotate it before expiry.

### 4. Store the secrets

In the `pr-digest` repo: Settings → Secrets and variables → Actions →
New repository secret. Add two:

| Name | Value |
|---|---|
| `GH_TOKEN` | The fine-grained PAT from step 3 |
| `MATTERMOST_WEBHOOK_URL` | The webhook URL from step 2 |

### 5. Test

Go to the Actions tab in the `pr-digest` repo, select "PR Digest",
click "Run workflow". Within ~30 seconds you should see the digest
message appear in the Mattermost channel.

### 6. Wait for the schedule

The default schedule is 09:00 UTC on weekdays. Edit the `cron:` line
in `.github/workflows/pr-digest.yml` to change it. Cron is in UTC.

## Files in this repo

```
.github/workflows/pr-digest.yml  # The schedule + run config
scripts/pr_digest.py             # Main script: fetches, renders, posts
scripts/business_hours.py        # Weekday-aware delta math
repos.yaml                       # List of repos to scan
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

## License

Pick one. Internal use, no default.
