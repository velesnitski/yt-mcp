# 071 — Score references by what tells candidates apart; eval in-process

## Context

A live re-test of ADR-070 confirmed per-key routing and the clear-lead rule.
It also found three problems:

- **Long phrases sank correct answers.** A spoken reference carries filler
  (meeting chatter, formatting remarks) and a shared meeting
  topic. The score divided the matched words by all of them, so a ticket
  whose summary held the one telling word scored below the floor. Every
  meeting reference came back `none`.
- **A said project key did not hold.** The key was tried first, but any
  answer short of `confident` sent the search to every project.
- **One shared word made a false `confident`.** A status line about today's
  outage matched an older ticket on the same component through a single
  distinctive word.

Checking releases also meant reading six to eight live tool outputs by hand.

## Decision

- **Vocabulary scoring for references.**
  - A ticket number already narrows the field to a handful of tickets.
    The words that count are those that occur in at least one candidate.
  - Words that match nothing in any candidate are dropped before scoring,
    so filler cannot dilute the score.
  - Free-text search keeps share-of-words scoring, because there is no
    number to narrow its field.
- **Said keys are the hint.**
  - A project key said in the reference or context is used for routing,
    never as a word to match.
  - The search stays with that key unless the key holds no such ticket or
    the words contradict it.
- **Owner as a share, not a capped bonus.** When `owner` is given, the
  score is 80 % words and 20 % owner. Two perfect word matches can then
  still be told apart by assignee, which a bonus capped at 1.0 could not do.
- **Two words, or one and now.** For free text, `confident` needs two
  distinctive words in the summary, or one on a ticket updated in the last
  three days. Otherwise the answer is `weak`, and `why` says so.
- **Machine-readable output.** JSON results of both tools carry `verdict`
  and `top`.
- **`yt-mcp eval <cases.json>`** runs a local case file in-process: one
  server start, cases in parallel, PASS/FAIL per case, exit 0 or 1.
  Case files hold real tickets and stay outside the repo. The format is
  documented in `evalcases.py` with an invented example.

## Consequences

- References spoken with long Russian context resolve again. The shapes of
  the live failures are mirrored as public tests on invented data. The live
  cases themselves run through `yt-mcp eval` against the operator's local
  case file before a release.
- Free text needs stronger evidence to be `confident`. A real but
  single-word match now arrives as `weak`, with the candidate shown.
- Each rule is pinned by a test and was checked against a mutant of the
  code it guards.

## Correction (1.40.1)

The single-word exception first keyed on `updated`. A live run showed an
older ticket being touched on the same day as the event it did not describe,
which made it look current. The exception now keys on `created`: one shared
distinctive word is enough only on a ticket opened in the last three days.
