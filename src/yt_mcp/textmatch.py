"""Match spoken or written phrases to ticket text, Russian and English (ADR-069).

Status checks name work by words ("the cache warmup on stage"), not by ID.
This module turns such a phrase into content stems and scores a ticket
against them, so callers can rank candidates and, just as important, refuse
weak ones. Light, dependency-free stemming: lowercase, ё→е, strip common
inflection endings, then compare stems allowing one to extend the other
("мониторинг" / "мониторинга", "deploy" / "deploying").

Deliberately conservative: a score explains itself (`why`) and the callers
apply a threshold, below which they answer "no confident match" rather than
the top of a noisy list.
"""

import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:[-_][0-9A-Za-zА-Яа-яЁё]+)*")
ISSUE_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*-\d+)\b")
# A bare number right after a word that names a ticket: "task 4102",
# "задача 4101", "тикет №55", "#77".
BARE_REF = re.compile(
    r"(?:\b(?:задач[аиеуой]*|тикет[аеуом]*|таск[аеуом]*|ишью|issue|ticket|task|bug|баг[аеуом]*)\s*(?:№|#|no\.?)?\s*|#)(\d{1,7})\b",
    re.IGNORECASE,
)

_STOP = frozenset("""
a an the and or of on in at to for from by with without into onto over under up down out is are was were be been
being this that these those it its as not no yes do does did done doing has have had will would can could should
may might must we you they he she i me my our your their them us what which who whom when where why how all any
some each every more most other such only own same so than too very just also still yet again about after before
there here then now today yesterday tomorrow week day days please team guys work working task tasks ticket issue
issues bug status update updates check see let lets via per
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня
еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там
потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже
себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас
были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего
них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
всегда конечно всю между это эта сегодня вчера завтра неделе неделю ребята команда задача задачи задачу задаче
тикет тикета статус работа работы работают работаем делаем сделать делать сделали занимаются занимается сейчас
""".split())

_RU_ENDINGS = sorted("""
иями ями ами ией иям ием иях ого его ому ему ыми ими ешь ете ите ала яла ила ела ола али яли или ели ются ется ться
ая яя ое ее ые ие ый ий ой ей ых их ым им ом ем ам ям ах ях ую юю ою ею ов ев ия ие ий ью ье ть ет ут ют ит ат ят
ся сь а я о е ы и у ю ь й
""".split(), key=len, reverse=True)
_EN_ENDINGS = ("ingly", "ations", "ation", "ments", "ment", "ings", "ing", "edly", "ies", "ied", "ers", "er",
               "ed", "es", "ly", "s")
_CYRILLIC = re.compile(r"[а-я]")


def stem(word: str) -> str:
    w = word.lower().replace("ё", "е").replace("-", "").replace("_", "")
    if _CYRILLIC.search(w):
        for end in _RU_ENDINGS:
            if w.endswith(end) and len(w) - len(end) >= 3:
                return w[: -len(end)]
        return w
    for end in _EN_ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[: -len(end)]
    return w


# Russian speech about English-titled tickets (ADR-070): a Russian stem
# also matches the English word it names. Keys are stem prefixes; kept to
# infrastructure and product vocabulary where the gap was observed.
_RU_EN = {
    "сервер": ("server",), "репозитор": ("repository", "repo"), "стейдж": ("stage", "staging"),
    "сет": ("network",), "подсет": ("subnet",), "депло": ("deploy",), "мониторинг": ("monitoring",),
    "прокси": ("proxy",), "контейнер": ("container",), "перезапуск": ("restart",),
    "обновлени": ("update", "upgrade"), "миграци": ("migration",), "сертификат": ("certificate",),
    "домен": ("domain",), "подключени": ("connection", "connect"), "ошибк": ("error",),
    "бэкап": ("backup",), "бекап": ("backup",), "баз": ("database",), "лог": ("log",),
    "алерт": ("alert",), "оплат": ("payment",), "подписк": ("subscription",),
    "уведомлени": ("notification",), "отчет": ("report",), "авторизаци": ("auth", "authorization"),
    "регистраци": ("registration", "signup"), "пользовател": ("user",), "приложени": ("app", "application"),
    "админк": ("admin",), "клиент": ("client",), "тест": ("test",), "прод": ("prod", "production"),
    "восстановлени": ("recovery", "restore"), "проверк": ("check",), "доступ": ("access",),
    "кеш": ("cache",), "кэш": ("cache",), "очеред": ("queue",), "релиз": ("release",),
}

# Words that name an environment, a platform, a generic part or a symptom
# ("no connection", "error"): they tell
# little about WHICH ticket is meant, so they weigh less and can never be
# the only evidence (ADR-070). Extend locally with YOUTRACK_GENERIC_WORDS.
_GENERIC = frozenset("""
dev stage staging prod production test qa client api server app application web site backend frontend
ios android mac macos windows linux desktop mobile build version release fix bug feature update new old
connect connection error crash problem fail failure broken issue work
""".split())


_RU_EN_STEMS: dict[str, tuple[str, ...]] = {}


def _generic_extra() -> frozenset:
    import os
    return frozenset(w.strip().lower() for w in os.environ.get("YOUTRACK_GENERIC_WORDS", "").split(",")
                     if w.strip())


def alternatives(word: str) -> set[str]:
    """The word's stem plus the stems of English words it translates to."""
    st = stem(word)
    out = {st}
    if _CYRILLIC.search(st):
        for ru, ens in _RU_EN_STEMS.items():
            # Short keys must match exactly: "прод" must not take "продажи".
            if st == ru or (len(ru) >= 5 and st.startswith(ru)):
                out.update(stem(e) for e in ens)
    return out


def is_generic(word: str) -> bool:
    """True when every part of the word ("client-api" → client, api) is a
    generic word, in either language."""
    generic = {stem(g) for g in _GENERIC | _generic_extra()}
    parts = [x for x in re.split(r"[-_]", word.lower()) if x] or [word.lower()]
    return all(alternatives(x) & generic for x in parts)


def content_words(text: str) -> list[str]:
    """Meaningful words in order, de-duplicated by stem; no stopwords, no
    pure numbers, no issue keys (those are resolved, not matched)."""
    text = ISSUE_KEY.sub(" ", text or "")
    out, seen = [], set()
    for m in _WORD.finditer(text):
        w = m.group(0)
        if w.isdigit() or len(w) < 2 or w.lower().replace("ё", "е") in _STOP:
            continue
        s = stem(w)
        if s not in seen:
            seen.add(s)
            out.append(w)
    return out


def stems(text: str) -> set[str]:
    return {stem(w) for w in _WORD.findall(text or "") if not w.isdigit()}


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long_.startswith(short) and len(long_) - len(short) <= 3


def _hits(wanted: list[str], have: set[str]) -> list[str]:
    return [w for w in wanted if any(_same(a, h) for a in alternatives(w) for h in have)]


@dataclass
class Match:
    score: float
    in_summary: list[str] = field(default_factory=list)
    in_body: list[str] = field(default_factory=list)
    distinctive: bool = False  # a non-generic word is in the summary

    def why(self) -> str:
        parts = []
        if self.in_summary:
            parts.append("summary: " + ", ".join(self.in_summary))
        if self.in_body:
            parts.append("text: " + ", ".join(self.in_body))
        return "; ".join(parts) or "no shared words"


def weight(word: str, idf: dict[str, float] | None = None) -> float:
    w = (idf or {}).get(word, 1.0)
    return w * (0.3 if is_generic(word) else 1.0)


def idf_weights(words: list[str], docs: list[str]) -> dict[str, float]:
    """Rarity of each word across the candidate set: a word every candidate
    shares tells them apart no better than none (ADR-070)."""
    import math
    n = len(docs)
    have = [stems(d) for d in docs]
    out = {}
    for w in words:
        df = sum(1 for h in have if _hits([w], h))
        out[w] = math.log((n + 1) / (df + 1)) + 1.0
    return out


def score(words: list[str], summary: str, body: str = "",
          idf: dict[str, float] | None = None) -> Match:
    """0..1: weighted share of the phrase's content words found in the
    ticket, a summary hit counting three times a description/comment hit.
    Generic words weigh 0.3; `idf` (from idf_weights) favours rare words."""
    if not words:
        return Match(0.0)
    in_summary = _hits(words, stems(summary))
    rest = [w for w in words if w not in in_summary]
    in_body = _hits(rest, stems(body))
    ws = {w: weight(w, idf) for w in words}
    # A long sentence must not make every match weak: the denominator counts
    # the six heaviest words only.
    denom = 3 * sum(sorted(ws.values(), reverse=True)[:6])
    value = min(1.0, (3 * sum(ws[w] for w in in_summary) + sum(ws[w] for w in in_body)) / denom)
    distinctive = any(not is_generic(w) for w in in_summary)
    return Match(round(value, 3), in_summary, in_body, distinctive)


_RU_EN_STEMS.update({stem(k): v for k, v in _RU_EN.items()})
