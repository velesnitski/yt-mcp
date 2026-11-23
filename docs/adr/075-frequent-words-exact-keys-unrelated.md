# 075 — Frequent words, keys whose words differ, unrelated candidates

## Context

Three problems came up in live use of the matching tools:

- **Frequent words made a false `confident`.** A phrase built from words
  that sit in most summaries of the searched projects matched an unrelated
  ticket on those words alone. Examples are a product or brand name, or a
  filler word such as "like". IDF over the candidate set did not help: the
  set is the union of one search per word, so a word that occurs everywhere
  still looked moderately rare inside it.
- **An explicit key was demoted.** A key that exists came back `weak`, with
  score 0 and "no shared words", when the words said around it did not fit
  its summary.
- **Unrelated candidates were listed.** A `weak` answer listed candidates
  with score 0 next to the real ones.

## Decision

- **A word whose own search fills a page is frequent.** In
  `find_issues_for_text`, each word is searched separately with a page of
  30. A word that fills the whole page occurs in too many tickets of the
  window to tell them apart.
  - It weighs like a generic word and is never the distinctive one.
  - The answer lists such words (`frequent_words`).
  - It costs no extra request.
- **Two new stopwords:** "like" and «типа».
- **An existing key is `exact`.** When the words around it do not fit, the
  answer says "words differ from the summary" (`words_differ: true`). The
  key is not demoted.
- **A `weak` answer lists only candidates that share a word.** The others
  are counted (`unrelated`, "+N unrelated").

## Consequences

- A phrase made only of frequent words gives `none` or `weak`, never
  `confident`. A real match still needs two distinctive words, or one on a
  ticket created in the last three days (ADR-071).
- In a busy window, a common word such as "payment" can count as frequent.
  A match then needs another specific word. This is intended: such a word
  does not identify a ticket.
- Tests in `tests/test_matching.py` pin each rule on invented data. Each was
  checked against a mutant of the code it guards.
