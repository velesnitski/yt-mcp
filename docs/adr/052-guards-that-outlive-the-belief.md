# 052 — Guards that outlive the belief that created them

## Context

Four defects this month share one shape, and it is not carelessness.

Q17 was recorded in the registry, cited from two ADRs, and then
hand-implemented wrongly twice — once in the deadline tools, once in the
pulse pipeline. Q2 was recorded, cited, and false: it asserted a clause
ordering the parser never required, and its evidence column pointed at a
code comment in the sibling repo that does not exist. `due date:` was
queried for months although no such attribute exists. ADR-025 declared
that a pinned tag plus a process kill made a reconnect run what shipped;
the live process ran a three-release-old pin while the config held the
new one.

The common cause is that **knowledge was recorded as prose and prose does
not execute**. Each entry named a "guard", but a guard that is a sentence
cannot fail. Worse, each of these was *verified* at the time, by a check
that could not have distinguished success from failure: Q2's fix changed
two variables at once and credited one; ADR-025's investigation stopped
after two plausible causes were repaired; the OR-clause sites each tested
the string their own author intended to emit.

## Decision

Three mechanisms, each aimed at one of those failures.

**A prober that measures the API instead of believing it.**
`scripts/verify_contract.py` sends every query shape the code builds and
compares the result against the registry's claim. It asserts **both
directions**: shapes the code relies on must succeed, and shapes an entry
calls rejected must still be rejected. That second half is the part that
matters — a negative control which starts passing is exactly how a stale
quirk announces itself, and is precisely what nobody ran for Q2. It reads
only counts, hard-codes no identifiers (project keys are discovered at
runtime, which also keeps a public repo clean), and runs on demand rather
than in CI, since it needs credentials CI does not have.

**Guards anchored to something that exists.** `test_quirk_registry.py`
parses the registry and fails when an entry names no checkable artifact.
On its first run it found three — entries claiming tests "in both repos"
without naming one. Those now name real tests, or state explicitly that
the guard lives in the sibling repo, or that the risk is accepted. The
same file pins the table's shape and requires a retraction to carry a date
and say what was re-measured.

**Shape guards over site guards.** Introduced in ADR-051 and generalized
here: check the code for the pattern the API refuses, not each call site
for the string its author meant to write. Per-site assertions passed
cleanly over both broken copies of the OR clause.

## Consequences

- One of these tests leaked on its first draft: the check that no real
  project key is hard-coded originally listed all sixteen of them in its
  own regex, publishing the internal key set in a public repo. It now
  checks behaviourally with sentinels. A guard that must name the secret
  to protect it is the wrong guard — and the sweep did not catch it,
  because bare keys are its documented blind spot.
- 955 tests. The prober's construction is unit-tested even though its
  execution is not: flipping its negative controls to positive turns two
  tests red, so a prober that measures nothing cannot ship quietly.
- Running the prober after a Cloud upgrade replaces "a tool broke, someone
  will report it" with a list of which shapes changed.
- The rule this encodes, and the reason it is written down rather than
  assumed: **a check that cannot fail is not evidence.** Every guard added
  from here is mutation-tested — reintroduce the defect, watch it go red —
  and every verification carries a positive control, because a grep that
  silently matches nothing and a passing test look identical in a
  terminal.
