# 069 — From words to tickets, and a batch fetch that never fails whole

## Context

Status checks and meeting notes name work by words ("the cache warmup on
stage") or by bare numbers ("task 1947"), not by ID. A client then wants the
ticket link. There were two ways to get it, and both were wrong in a
confident way:

- A free-text `search_issues` returns the top of a noisy list as though it
  were an answer (ADR-047).
- Trying a bare number under one project key finds *a* ticket, often the
  wrong one. With several instances, the same number exists in several
  projects. One live case resolved to a real but unrelated ticket on the
  default instance, while the right one sat on the second instance.

Separately, `get_issues` composes one `#A or #B` query. A single unknown or
malformed ID made YouTrack refuse the whole query, so callers lost every
issue in the batch.

## Decision

- **`textmatch`** is a small RU+EN matcher with no dependencies:
  - Stopwords are dropped, and words are reduced to stems by stripping
    common endings, so inflected forms meet.
  - Two stems match when one extends the other by a few characters.
  - A ticket's score is the share of the phrase's content words it
    contains, with a summary hit worth three times a description hit.
  - Every score carries a `why`.
- **`list_instances`** returns every instance's base URL, its project keys,
  and which instance is the default.
- **`resolve_ticket_refs`** verifies by meaning, not by existence.
  - A bare number is tried under each likely key, in this order:
    - routes whose context word appears near the number;
    - the remaining routes;
    - without routes, every project of every instance (or of the hinted
      instance), capped.
  - Routes come from `YOUTRACK_REF_ROUTES=word:instance:KEY,…`, which is
    local configuration and never committed.
  - Every hit is scored against the words near the number. An owner who
    matches the assignee adds weight.
  - Possible results:
    - `confident`: the best scores at least 0.5 and beats the runner-up by
      at least 0.2;
    - `weak`: the top three candidates, for a person to pick from;
    - `candidates`: no words were given, so the list is unverified;
    - `exact`: an explicit key that exists and is not contradicted;
    - `none`: the existing tickets with that number are named in a note
      but not offered as the answer.
- **`find_issues_for_text`** handles a whole digest in one call.
  - Explicit keys, and "task 123" references within the given projects,
    are resolved directly.
  - Other texts run one scoped search per content word, the results are
    unioned and ranked locally, with small bonuses for open and recently
    updated tickets.
  - Below 0.34 the answer is "no confident match", never the top result.
  - One query per word means recall does not depend on how YouTrack treats
    a multi-word phrase.
- **`get_issues`:**
  - Non-ID tokens never reach the query.
  - Duplicate IDs collapse into one.
  - If the composed query is refused, each ID is fetched on its own, in
    parallel.
  - Results keep input order. Each missing ID gets its own item (`not found
    or no access`, or `not an issue ID`). JSON items carry `url`.
  - A permission error is never masked by the fallback.

## Consequences

- A digest becomes ticket links in one call, and it says when it does not
  know.
- A bare number costs one parallel round of single-issue reads per likely
  project. The cap and the routes keep that small.
- The thresholds are tuned against fixtures, not yet against live data.
  Each output carries `score` and `why`, so a caller can apply a stricter
  bar.
- `tests/test_matching.py` pins every rule above, including ties, the
  route order, Russian inflections and the batch fallback. Each rule was
  checked against a mutant of the code it guards.
