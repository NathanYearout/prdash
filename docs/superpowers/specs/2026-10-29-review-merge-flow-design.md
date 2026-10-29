# Review and merge flow: changes since your review, merge blockers, request review

## Why

The author's workflow at Payload: write the code, push, wait for CI, then request review from the
`payload-devs` team on GitHub and post the PR to Slack with prdash. Someone else merges. On personal
projects the same person also merges. prdash covers the Slack step today. Waiting for CI, requesting
review, re-reading PRs that come back, and working out why a PR can't merge all happen on GitHub.

Everyone gets their PRs reviewed, and people who merge also prepare PRs. So nothing here is split
by role. Every feature is available to everyone, and a merge action shows up only where GitHub says
the viewer can use it.

## Goals

1. **Changes since your last review.** When a PR you reviewed gets new commits, the card says so
   and links straight to only those changes. The AI review can run on just that range.
2. **Why it can't merge.** Every open PR shows what's stopping it from merging (behind base, needs
   approval, required checks, branch rules), next to the action that fixes it.
3. **Merge when ready.** Turn on GitHub auto-merge from prdash wherever GitHub allows it.
4. **Request review.** Request review from people and teams from the PR panel, with how busy each
   person is, an option to wait until CI is green, and an option to post to Slack in the same step.

## Non-goals

- A setting that says what role you have, or which orgs you merge in. GitHub's own permission
  fields decide what shows.
- Automatically choosing reviewers, CODEOWNERS parsing, merge queues.
- Reading branch protection rules to say "needs 2 approvals". That API needs admin access. The
  blocker comes from `mergeStateStatus` and `reviewDecision`.
- A diff viewer inside prdash. Links go to GitHub's files view.

## Anti-goals (would count as failure even if it works)

- Posting a personal project to the work Slack channel without being asked.
- Requesting review on a PR whose CI is red because a wait-until-green was armed and then
  forgotten.
- Adding new columns or moving cards between columns. The board's columns stay as they are, and
  blockers are chips.

## Constraints

- Standard library only, everything through `gh`, as now. No new dependencies.
- Demo mode (`prdash/demo.py`) has data for every new field so the board, the screenshots, and
  e2e can show them. Actions stay disabled in demo, as now.
- New PR fields come through the existing sync (`PR_FIELDS` → `flatten()`); no extra request per PR
  per sync.
- Tests: unittest for the backend logic (blocker text, the wait-until-green states, the request
  call), e2e for the picker. Keep to the existing style: one small check per behavior.
- README: feature rows, filter table, keyboard table.

## 1. Changes since your last review

- Add `commit { oid }` to `latestReviews` in `PR_FIELDS`, and keep it on each `reviewers` entry
  (`{who, state, commit}`).
- **Anchor:** the viewer's latest APPROVED or CHANGES_REQUESTED review; only if they have neither,
  their latest COMMENTED review. Thread replies create COMMENTED reviews, so they must not move the
  anchor past a real review. Verify on a real PR what `latestReviews` returns for someone who
  approved and later replied in a thread; if it returns the reply, fetch
  `reviews(last: 30, states: [APPROVED, CHANGES_REQUESTED])` instead and pick per author.
- **Shown when** the PR is open, isn't the viewer's own, the viewer has an anchor review, and
  `head` differs from that review's commit. A rebase changes `head`, so the chip also shows after
  a rebase with no new work. Accepted (my call): the reviewer can glance at the range and move on.
- **Card:** a chip, "New since your review", linking to `{url}/files/{review_commit}..{head}`
  (GitHub's compare-in-PR view). No commit count; it would need another API call.
- **Panel:** the same link next to Files, and a **Review changes since mine** button. It runs the
  existing AI review with the compare diff (`repos/{o}/{r}/compare/{a}...{b}` as a diff) in place of
  the full PR diff, plus the viewer's own earlier feedback: their review bodies and their inline
  thread comments (from `conversation()`, which already fetches threads). The prompt asks "were
  these comments addressed, and is anything new wrong?". Comments and description are still sent
  as now.
- **Rebase or force-push:** call the compare API as JSON first. If `status` isn't `ahead` (the
  reviewed commit isn't an ancestor of head), use the full PR diff, and the output starts with a
  line saying the branch was rewritten so this is a full review. A branch that merged base in
  stays `ahead` and its diff includes base's changes; accepted.
- **Filter:** `is:new-since-review`.
- If the compare call errors (commit gone), same fallback as a rewritten branch.

## 2. Why it can't merge

- Add `mergeStateStatus` to `PR_FIELDS`. It may need the `merge-info-preview` Accept header on
  `gh api graphql`; check on a real PR and add the header if the field comes back null or errors.
- One function, `blocker(p)` in `github.py` next to `status()`, returns short text or `None`. It runs
  for open, non-draft PRs:
  - `DIRTY` → nothing new (conflicts already put it in Needs work).
  - `BEHIND` → "Behind {base}".
  - `BLOCKED` and `reviewDecision == REVIEW_REQUIRED` → "Needs approval".
  - `BLOCKED` and CI not passing → "Required checks". (CI here and in wait-until-green means the
    whole rollup, every check, the same as the board's Needs work today. Required-only is out of
    scope; my call.)
  - `BLOCKED` otherwise → "Blocked by branch rules".
  - `CLEAN`, `HAS_HOOKS`, `UNSTABLE`, `UNKNOWN` → `None`.
- **Card:** a flag chip with the blocker. Columns don't change.
- **Panel, Manage section:** the blocker line, with the matching action next to it: Rebase on
  base for "Behind", Request review for "Needs approval". The other blockers link to the checks or
  to GitHub.
- **Filter:** `is:blocked` (any blocker), `is:behind`.

## 3. Merge when ready

- Add `autoMergeRequest { enabledAt mergeMethod enabledBy { login } }`, `viewerCanEnableAutoMerge`,
  `viewerCanDisableAutoMerge` to `PR_FIELDS`.
- **Ops:** `automerge` → `gh pr merge --auto --{merge_method}`; `automerge_off` →
  `gh pr merge --disable-auto`. Both are in `CHANGES_GITHUB`.
- **Panel, Manage section:** **Merge when ready** next to Merge when `viewerCanEnableAutoMerge`.
  When it's on: "Auto-merge on ({method}) · Cancel" when `viewerCanDisableAutoMerge`, otherwise
  just the text. Repos without auto-merge turned on get `viewerCanEnableAutoMerge = false`, so the
  button doesn't show.
- **Card:** an "Auto-merge" chip while it's on.
- **Filter:** `is:auto-merge`.
- The existing merged event in the inbox covers when it finishes.
- Only the new button is gated by GitHub's permission field. The existing Merge, Rebase,
  Ready/Draft and Close buttons stay as they are (my call).

## 4. Request review

### The picker

- **Where:** a **Request review** button in the panel's action row for open PRs. A keyboard key
  and a palette entry too; the key is the builder's pick from what's free.
- **Teams:** the PR's org's teams from `gh api orgs/{org}/teams --paginate`, cached per org in the
  db for a day. A personal account has none, so the section is hidden.
- **People:** the people prdash already knows in that org (the existing `people` list filtered to
  PRs in that org), plus collaborators for repos owned by a user
  (`repos/{o}/{r}/collaborators`, cached the same way). Never the PR's author. A text search box at
  the top filters both lists.
- **Load:** next to each person, the number of open, non-draft PRs that currently request them,
  counted from synced PRs. Teams get the same count. Shown only when the team view is on, because
  without it the count only sees your own PRs and would be wrong. Hover text says what it counts.
- People and teams already requested show as checked. Unchecking someone is out of scope; GitHub
  handles removal.

### Options under the list

- **When CI is green.** Defaults to on when CI isn't passing yet, and hidden when it already is.
- **Post to Slack.** Shown when Slack is set up and the PR hasn't been posted yet.
- **Remembered per owner (org or user):** the last teams, people, and Slack choice, stored in the
  db under one key, `request_prefs = {owner: {teams, users, slack}}`. The first Payload PR asks;
  every later Payload PR opens with `payload-devs` and Slack already ticked. A personal repo
  remembers its own choice, so the work Slack only ever gets posts you ticked for that owner.

### The request call

- One function, `request(pr, users, teams)` in `github.py`: POST `requested_reviewers` with
  `reviewers[]` and `team_reviewers[]`. The existing `rerequest` becomes a call to it with its
  current "already a reviewer" check kept.

### Waiting until green

- Arming stores `request_when_green = {users, teams, slack, at, skip}` in the PR's local row. It
  keeps waiting across pushes. `skip` is the head at arm time if CI was already failing then,
  otherwise null: failures on the `skip` head are ignored, so arming on a red PR means "after I push
  a fix and it goes green".
- `requests_when_green()` in `sync.py`, called right after `retries()`, for each armed open PR:
  - `head == skip` → wait.
  - Checks running → wait.
  - No checks on head: if the repo has ever had checks (any row in `check_state` for the repo),
    CI hasn't started yet → wait. If it never has, the repo has no CI → treat as passing.
  - All checks passing → if the PR is a draft, mark it ready; then
    request; then post to Slack if ticked and not already posted; disarm; inbox event
    "Requested review from payload-devs and posted to Slack: {label}".
  - A check failed and Retry until green is armed on the PR → wait for it.
  - A check failed otherwise → disarm; inbox event "CI failed on {label}; didn't request review".
  - A GitHub or Slack error → disarm and put the error in the inbox event. Whatever already
    succeeded stays done.
  - PR merged or closed → disarm silently.
- **Card:** a chip, "Review when green", while armed. The panel shows the same thing with Cancel.
- **Drafts:** the button reads "Mark ready and request when green" so the draft step isn't a
  surprise.
- **Filter:** `is:requesting` (armed).

## Left to the builder

- Exact chip wording and icons, the keyboard key, picker layout.
- Whether the picker is a popover or a section in the panel.
- How to share code between `retries()` and `requests_when_green()`.
- Demo data shape for the new fields.
- Splitting this into commits: the four parts are independent and can land one at a time, in the
  order above.
