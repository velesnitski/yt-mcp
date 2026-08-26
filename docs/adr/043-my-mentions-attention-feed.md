# 043 — get_my_mentions: an attention feed over flat comments

## Context

"Did anyone answer or mention me?" previously took a manual combination:
a `mentions: me` query (which also matches issue text and stale mentions
in recently-updated issues) plus comment archaeology, and the result was
dominated by notification-template pings — deadline-nag reposts that
carry HUMAN authorship, so the ADR-037 service filter (login-prefix +
text-prefix based) cannot catch them: the template marker sits
mid-comment. YouTrack comments are also flat — "replied to me" does not
exist as a structure and can only be approximated.

## Decision

New tool `get_my_mentions(days=14, max_results, instance)` (tool #84):

- Merges two windowed queries: `mentions: me updated: <range>` and
  `commenter: me updated: <range>`.
- Classifies each recent comment by someone else:
  - **Mention** — the text names me. Needles are the login, display name,
    and surname, all normalized alphanumeric/case-insensitive so
    `@First_Last`, `@FirstLast` and bare surname all match; needles
    shorter than 3 chars are dropped (a one-letter surname otherwise
    degenerates into a match-everything substring — caught by the test
    suite on first run).
  - **Possible reply** — newer than my latest comment on an issue I
    commented in; explicitly labeled a heuristic.
- Noise handling: the ADR-037 service filter plus a body-marker rule for
  notification templates ("FYI @" mid-text). The dropped count is always
  reported — the anti-silent-zero convention.
- Window guard raises UserInputError (ADR-036, Sentry-filtered).

## Consequences

- One call answers "what needs me", per instance; mentions outrank the
  reply heuristic for the same comment.
- 84 tools; 868 tests pass (10 new, generic fixtures only). Minor
  release 1.22.0.
