# Branch protection - `main`

Applies to `<owner>/raveplayer-web` -> `main`.

`main` is what gets built, deployed or published. It should only move through a
reviewed PR whose checks passed, so a stale or unverified commit can never
become the live version.

## Required settings

| GitHub setting | Value | Why |
|---|---|---|
| Require a pull request before merging | On | No direct pushes to `main` |
| Required approvals | 1 | One review from the owning workspace |
| Dismiss stale approvals | On | New commits need a fresh look |
| Require review from Code Owners | Off | Single-owner repos; the reviewer is chosen per PR |
| Require conversation resolution | On | Claim rows and questions get closed, not lost |
| Require status checks to pass | On | CI is the evidence |
| Require branches to be up to date | On (`strict`) | No merging a green branch that is behind `main` |
| Required checks | `test`, `browser`, `image` | From `.github/workflows/` |
| Block force pushes | On | History rewrites only by explicit decision |
| Block deletions | On | `main` must always exist |
| Enforce for admins | On | The owner follows the same rule |
| Require signed commits | Off | Not configured; do not add a gate nobody can pass |
| Require linear history | Off | Merge commits are fine |

## Owning workspace review

| Repo | Owner (review + merge) |
|---|---|
| `control-centre` | CC workspace |
| `raveplayer` (Kodi) | Rave workspace |
| `Rave Player` (web) | Rave workspace |

Cross-workspace PRs: the producer opens the PR, the owning workspace reviews.
Changes that touch the Rave stats/actions/playback contract need both sides to
ack in the PR thread before merge.

## Required status checks

Required: `test`, `browser` and `image` (workflow `build`). `browser` runs the Playwright smoke suite on ubuntu-24.04 (Playwright 1.60 does not support ubuntu-26.04); `image` builds and smoke-tests the Docker image.

Add a check name only after it has run at least once on a PR - GitHub only
offers checks it has already seen. If a job is renamed in the workflow, update
branch protection in the same change.

## Set it up (web UI)

1. **Settings -> Branches** (or **Rulesets -> New branch ruleset**), scope to
   `main` only. Do not protect tags or release branches.
2. Add the settings in the table above.
3. Under status checks, search for each of `test`, `browser`, `image` and select it. Tick
   **Require branches to be up to date before merging**.
4. Enable **Do not allow bypassing the above settings** (with rulesets: leave
   the bypass list empty).
5. Save, then work through the verification checklist below.

## Set it up (gh CLI)

```bash
gh api -X PUT "repos/<owner>/raveplayer-web/branches/main/protection" \
  -H "Accept: application/vnd.github+json" --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["test", "browser", "image"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "required_conversation_resolution": true,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

GitHub CLI is not required for day-to-day work; the web UI is the source of
truth and this command is only a shortcut for the same settings.

## Admin bypass (documented, emergency only)

- Normal rule: `enforce_admins: true`. Even the owner cannot push to `main`.
- Emergency only: temporarily clear **Do not allow bypassing** (or add the
  owner to a ruleset bypass list) to merge a security fix when the normal path
  cannot work. Merge the same reviewed commit; do not write a new one.
- Every bypass is recorded in `docs/COORDINATION.md` under Deploys with: why,
  commit, timestamp, and rollback. Re-enable the setting immediately.
- Never bypass to skip a review, skip a failing check, or push a version bump.

## Verification checklist

- [ ] `git push origin main` is rejected for the owner.
- [ ] A PR with no approval shows **Review required** and cannot merge.
- [ ] A failing or missing required check blocks merge.
- [ ] A PR branch behind `main` shows **This branch is out of date** and cannot
      merge until updated.
- [ ] Force-pushing `main` and deleting `main` are both rejected.
- [ ] The admin account cannot merge without approval.

## Notes

- Private repos need GitHub Pro/Team/Enterprise for branch protection; public
  repos work on Free. If this repo is private, use Rulesets on a paid plan or
  keep the same rules by convention and note that here.
- Branch protection stops bad merges; it does not stop two workspaces editing
  the same area. The claim row in `docs/COORDINATION.md` does that.
