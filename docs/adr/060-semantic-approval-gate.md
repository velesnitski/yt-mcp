# 060 — Semantic gate: review guidance, not a merge bypass

## Context

Anthropic's write-up on redesigning source control for agent-scale
development describes review tooling that "can express complex semantic
constraints on approvals" — telling a comment-only change apart from a
sensitive logical one — while team members "still review every change
request… but they now examine the intent of the change". Two properties
carry that design: the constraints are enforced by the source-control
server, outside the change being judged, and human review of intent is kept
for every change.

A first port to this repository kept the vocabulary and lost both
properties. It classified pull requests into block / high / medium / low,
auto-merged `low`, waived review for owner-authored `high`, and proposed
dropping the required review count from 1 to 0. Tested against its threat
model rather than its syntax:

- It ran under `pull_request`, which executes the workflow from the pull
  request's own merge commit, and read its policy from the same checkout —
  so a pull request could rewrite the gate or the rules judging it.
- Its `high` path counted any `APPROVED` review not by the author; on a
  public repository any GitHub account can leave one.
- A path matching no rule defaulted to `low`. So did every path when its
  YAML tool was unavailable — the loop that read the policy swallowed the
  error. That made `CLAUDE.md`, `README.md` and `.gitignore` auto-mergeable.
- It downgraded "whitespace-only" diffs to `low` using
  `git diff --ignore-all-space`. In Python indentation is syntax: moving one
  `return` under a guard changed behaviour and counted as zero changed
  lines. "Comment-only" fails the same way here, where tool docstrings are
  the descriptions the model reads.
- Fork pull requests get a read-only token, so its labelling step would
  fail every external pull request.
- It could not constrain the main actor at all: releases push straight to
  `main` with admin bypass, and the owner waiver would cover agent-authored
  commits, which land under the owner's account.

The scale argument behind the original design also does not transfer: it
answers tens of thousands of merges a day. This repository has one
maintainer and about one merge a day.

## Decision

Keep the useful idea — classify by semantic risk, and refuse leaked
strings — and enforce nothing a pull request could influence.

- **Runs from the base branch.** `pull_request_target` executes the
  workflow and the classifier from the base branch. Nothing from the pull
  request is checked out or executed; changed files and patches come from
  the API and are read as data. No PR-controlled text is interpolated into
  a shell step. The checkout is pinned to a commit and keeps no
  credentials.
- **Guidance, not permission.** Levels are labels plus a job summary naming
  the paths that drove them. Nothing approves or merges. Native required
  review — which counts only users with write access — remains the only
  approval that matters. Branch protection is unchanged; there is no auto-
  merge and no owner waiver.
- **Fail-closed.** A path takes the highest level any rule assigns it, and
  an unmatched path takes the policy `default` (`medium`), never the lowest.
  A malformed policy, a mistyped key, an invalid pattern or a missing
  pattern list is an error, not a pass. There are no downgrade rules.
- **`block` is the only failing outcome.** Added lines, the title and the
  body are swept against the private pattern list held in a repository
  secret. Only a count is reported: the list is private and these logs are
  public. Files the API returns without a diff (binary or very large)
  cannot be swept and raise the level to `high`. Dependency-bot pull
  requests run without secrets and are classified by path only.
- **Owner pushes are untouched.** The workflow has no `push` trigger and no
  setting is loosened, so the release flow's direct pushes keep working
  through the existing admin bypass.

The classifier is a stdlib Python script rather than inline shell, so its
behaviour is unit-tested and needs no external tool on the runner.

## Consequences

- Tests pin the classifier and the workflow's safety conditions: base-branch
  execution, no head checkout, pinned actions, no PR text in shell steps,
  the secret confined to one step and never passed on a command line, and
  nothing that approves or merges. Each original flaw was re-introduced as
  a mutation and each turns a test red.
- Writing those tests found a defect in the rework itself: a filename
  containing a newline escaped `.github/**` because `.` stops at a newline
  and `$` matches before a trailing one. Patterns now compile with DOTALL
  and anchor with `\Z`.
- Not yet established: whether a `pull_request_target` check run attaches to
  the pull request in a way branch protection can require. It should run on
  a real pull request before being added to the required checks.
- Path rules live in `.github/risk-policy.json`; `.github/**` is itself
  `high`, so changing the policy is always flagged.
