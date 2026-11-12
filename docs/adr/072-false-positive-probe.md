# 072 — Rules from a live false-positive probe

## Context

A deliberate probe of the matching tools fed them phrases whose honest
answer is "no ticket", and numbers paired with words that contradict every
ticket carrying them. Most came back `none` or an honestly `weak` list. Three
did not:

- A ticket number with a single generic word ("test", "server") produced
  `weak` candidates whose only shared word sat in a description. That is
  noise presented as a lead.
- A status phrase was matched `confident` to a ticket cancelled weeks
  earlier. It shared two specific words, but a status line is about live
  work.
- The English stemmer reduces "worker" to "work", and "work" was on the
  generic list, so a specific word was treated as generic.

## Decision

- **A lead needs one specific word.** A candidate is offered as `weak` only
  if at least one non-generic word matched it, in the summary or the body.
  Generic-only overlap is `none`.
  - One exception: the reference named the project, and the number exists
    in it. Generic words then do not contradict that identity, and the
    result is `weak`, with `why` saying so. It is never `confident`.
- **Closed is offered, not asserted.** In `find_issues_for_text`, a ticket
  resolved more than seven days ago cannot be `confident`. It is shown as
  `weak`, with its state in `why`. A ticket closed in the last week still
  can be confident: a status line often reports work just finished.
- **"work" is no longer a generic word.** It collided with real words
  through stemming.

## Consequences

- Fewer leads are offered on thin evidence. A said project key keeps the
  case where the number alone identifies the ticket.
- Spoken references to completed work still resolve. Resolution of refs
  (`resolve_ticket_refs`) does not apply the closed rule, because meetings
  routinely cite finished tickets by number.
- Tests in `tests/test_matching.py` pin each rule on invented data. Each was
  checked against a mutant of the code it guards.
