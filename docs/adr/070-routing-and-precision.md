# 070 — Route IDs to their instance; match on distinctive words

## Context

A live run of the ADR-069 tools confirmed the safety property: a ticket
number that exists, but under the wrong project, is no longer offered as
the answer. It also found gaps in recall and precision:

- `get_issues` sent every ID to one instance. A key that lives on another
  instance came back "missing".
- A project key said in the context ("the PRJB team") did not route the
  number. Every project of every instance was still tried.
- Russian speech about tickets with English summaries matched nothing.
  "репозитории" never meets "repositories".
- A clear winner, for example 0.42 against 0.17, stayed `weak` because the
  absolute bar of 0.5 was not met.
- Generic words produced false matches. Environment, platform and symptom
  words ("dev", "client", "no connection") outweighed the one distinctive
  word, such as a place or a component name, that matched nothing.
- `find_issues_for_text` reported `words`, on a different scale from
  `resolve_ticket_refs`.

## Decision

- **`get_issues` routes by key.**
  - Each ID goes to the instances whose project list holds its key. The
    asked-for or default instance leads.
  - A key present on several instances is tried on each in turn, only for
    the IDs still missing.
  - JSON items say which `instance` they came from when more than one is
    involved.
- **Said keys route first.**
  - A project key or instance name in the reference or its context forms
    a first phase of tries.
  - The search stops there when that phase is already confident, and
    widens to every project otherwise.
  - `checked` names the instance when one ID was tried on two instances.
- **RU→EN bridge.** A small table of Russian infrastructure and product
  stems maps each to the English words it names (сервер → server, репозитор
  → repository, прокси → proxy, …). Stems under five letters must match
  exactly, so "прод" never claims "продажи".
- **Distinctive evidence.**
  - Generic words (environments, platforms, symptoms), plus any listed in
    the local `YOUTRACK_GENERIC_WORDS`, weigh 0.3.
  - Across a candidate set, words are weighted by rarity (IDF).
  - `confident` requires at least one non-generic word in the winner's
    summary.
  - `find_issues_for_text` drops candidates without one.
- **Clear lead.** A winner is `confident` when it either:
  - scores at least 0.5 and leads by 0.2, or
  - clears 0.34 and doubles the runner-up. Its `why` then says so.
- **One scale.** `find_issues_for_text` answers `confident` / `weak` /
  `none`, or `exact` for an explicit key, like `resolve_ticket_refs`.

## Consequences

- Batches that mix instances come back whole. Each missing ID costs one
  more batch per additional instance that holds its key.
- Precision rises: generic words alone cannot produce a match, and a place
  or a component name has to show up. Recall on Russian phrasing depends on
  the bridge table, which is small on purpose. Extend it from observed
  misses, not in advance.
- `tests/test_matching.py` adds regression tests for each live finding, on
  invented keys and summaries. Each rule was checked against a mutant of
  the code it guards.
