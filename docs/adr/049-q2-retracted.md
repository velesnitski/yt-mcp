# 049 — Q2 retracted: clause order was Q1 misattributed

## Context

Registry Q2 has said since July that a composed query must lead with its
`resolved date:` range, or the parser rejects it. It shaped real code: the
release calendar emits the range first with a comment calling it a parser
quirk, `contract.py` built a helper whose stated job was to make the
ordering "impossible to get wrong", two tests pinned it, and ADR-039
recorded it as a finding. The reporting repo carries the same row.

Reading that repo for an unrelated task turned up three of its queries
composing the range **last** — and they have been running daily without
complaint. The registry also credited Q2 to "reports verified 2026-07-28,
comment at query site". No such comment exists there. An entry claiming a
parser rejection, contradicted by code that runs every day, with
supporting evidence that cannot be found, is worth re-testing rather than
trusting.

## Decision

Retested live against Cloud, read-only:

| Query | Count |
|---|---|
| `project: PROJ resolved date: A .. B` | 62 |
| `resolved date: A .. B project: PROJ` | 62 |
| `summary: Release resolved date: A .. B` | 18 |
| `resolved date: A .. B summary: Release` | 18 |
| four clauses with `sort by:`, both orders | 0 and 0 |

Ordering makes no difference, including in the exact composition that
produced the original incident. The zero pair agreed, so that zero is the
data rather than an ordering artifact.

The variable was the attribute, not the order. `resolved:` still fails —
in **both** orders:

    resolved: A .. B project: PROJ   -> 400
    project: PROJ resolved: A .. B   -> 400

So the change that "fixed" the original failure switched `resolved:` to
`resolved date:` *and* moved the clause, and the credit went to the move.
Q1 was the whole story; Q2 was its shadow.

Q2 is retracted in place, in both repos, keeping the ID — it is cited from
code comments, tests and ADRs, and a vanished ID reads as an editing slip
rather than a decision. The registry's update protocol gains a fourth
rule: retract by rewriting, state what the retest measured, and only on
evidence that isolates a single variable.

The range-first output stays everywhere it already exists. It is harmless
and deterministic, and churning four call sites to prove a point would
risk more than it returns. What changes is what the code *claims*: a
convention is now labelled a convention.

## Consequences

- One fewer false constraint on every future query site in two repos.
- 929 tests pass; the ordering assertions remain but now describe output
  formatting rather than a parser requirement.
- The retest is the entry's new guard, with its numbers recorded above so
  the next person can re-run it rather than re-derive it.
- Generalizable: a guard that pins a rule nobody has re-tested is
  indistinguishable from a guard that pins a mistake. Q2 survived because
  its "verification" was a second change in the same commit.
