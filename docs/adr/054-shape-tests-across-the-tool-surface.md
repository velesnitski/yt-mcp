# 054 — One suite over every tool, instead of one suite per module

## Context

ADR-053 set the rule: cover by blast radius. Applying it per module works
but scales badly — a tool added next month is covered only if someone
remembers to extend the right file, which is how two modules sat near zero
while their neighbours were well tested.

So the bulk of this round is a single suite parameterised over the **live
registry**. Every registered tool is called against four payload shapes:
an empty collection, an empty object, a null-membered issue, and a
populated one. A tool registered tomorrow is covered the day it appears.

The null fixture is deliberately faithful rather than maximally hostile.
An earlier draft nulled `created`, `tags` and `customFields` too, which
the API never does — chasing those would have meant hardening code against
payloads that cannot occur. It nulls what registry Q6 documents, plus what
YouTrack genuinely returns for an unset field.

## Decision

Four cross-cutting properties, plus targeted suites for the write paths
that reading turned up bugs in.

The suite found 43 crashes. Two thirds were one idiom:

    issue.get("name", "")      # None when the key exists and is null
    (issue.get("name") or "")  # what was meant

`dict.get`'s default applies only when the key is **absent**. YouTrack
sends the key with a null value, so the default never fired and the None
flowed into `.lower()`, a comparison, or a join. 157 call sites.

Three remaining crashes were individual: an activity without a timestamp,
a null `field` object, and an unresolved issue's `resolved: null` compared
against a number.

Targeted work, from reading rather than measuring:

- **sprints** — `_find_sprint` matched substrings with no ambiguity check,
  so `"Sprint 1"` could silently target `"Sprint 10"`, aiming a write at a
  sprint nobody named, while `_resolve_board` directly above it already
  refused ambiguity. Exact name now wins; an ambiguous substring is an
  error. Dates raise `UserInputError` instead of a bare `ValueError` that
  error reporting would not filter (ADR-036), the create POST asks for a
  fields selector (Q16), and a transport failure no longer aborts a batch
  and discards the partial report.
- **articles** — the create POST had no selector, so callers were handed
  an internal id; and `delete_article` truncated the body at 500
  characters under a heading promising the output was enough to restore.
  It now reproduces a short article in full and states exactly how much it
  dropped from a long one, in words that cannot be read as a backup.

## Consequences

- Coverage 56% → 78%; `impact` 5→76, `dashboard` 6→61, `discovery` 8→66,
  `bulk` 10→76, `articles` 13→70, `journey` 27→76.
- 1,298 tests, all passing, with no existing behaviour changed.
- **A mechanical fix went wrong first, and that is the lesson worth
  keeping.** The first pass at the 157 call sites used a regex whose
  receiver pattern allowed `(` and `[`, so it matched from inside an
  enclosing call: `merged.setdefault(issue.get("idReadable", "?"), issue)`
  became `(merged.setdefault(...), issue)` — `setdefault` lost its second
  argument and started storing `None`. Two more sites broke syntactically
  and were obvious; that one was silent and broke 18 tests. The change was
  reverted wholesale and redone with the receiver restricted to plain
  identifiers. A bulk edit needs a check that fails loudly, and the test
  suite was that check.
