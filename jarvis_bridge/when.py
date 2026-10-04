"""Italian date/time expressions → datetime, deterministic.

``parse(text)`` finds *one* moment in free text ("domani alle 9 e mezza",
"tra due ore", "lunedì prossimo pomeriggio", "il 15 marzo alle 18",
"stasera", "fra mezz'ora") and returns it together with the text left over
once the time expression is removed, so the caller can use the remainder as
the reminder's subject.

No model is involved: a bounded set of regular expressions, all documented
below. Anything not recognised is simply left in the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_NUM_WORDS = {
    "zero": 0, "un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7,
    "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12, "tredici": 13, "quattordici": 14, "quindici": 15,
    "sedici": 16, "diciassette": 17, "diciotto": 18, "diciannove": 19, "venti": 20, "ventuno": 21, "ventidue": 22,
    "ventitre": 23, "ventitré": 23, "venticinque": 25, "trenta": 30, "quaranta": 40, "quarantacinque": 45,
    "cinquanta": 50, "sessanta": 60,
}
_WEEKDAYS = {"lunedì": 0, "lunedi": 0, "martedì": 1, "martedi": 1, "mercoledì": 2, "mercoledi": 2, "giovedì": 3, "giovedi": 3,
             "venerdì": 4, "venerdi": 4, "sabato": 5, "domenica": 6}
_MONTHS = {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
           "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12}
_WEEKDAY_NAMES = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_MONTH_NAMES = [None, "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

# Default hours for parts of the day when no clock time is given.
_PART_OF_DAY = {"mattina": 9, "mattino": 9, "pomeriggio": 15, "sera": 20, "notte": 22, "pranzo": 13, "mezzogiorno": 12, "mezzanotte": 0}

_WORDS_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _NUM_WORDS), key=len, reverse=True)) + r")\b", re.IGNORECASE)

# Idioms rewritten before number words become digits ("un quarto" must not become "1 quarto").
_IDIOMS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bun\s+quarto\s+d['’]\s*ora\b", re.IGNORECASE), "15 minuti"),
    (re.compile(r"\bmezz['’]?\s*ora\b|\bmezzora\b", re.IGNORECASE), "30 minuti"),
    (re.compile(r"\bun['’]\s*ora\b", re.IGNORECASE), "1 ora"),
    (re.compile(r"\be\s+un\s+quarto\b", re.IGNORECASE), "e 15"),
    (re.compile(r"\bmeno\s+un\s+quarto\b", re.IGNORECASE), "meno 15"),
    (re.compile(r"\be\s+mezz[ao]\b", re.IGNORECASE), "e 30"),
]

# --- relative: "tra/fra 10 minuti", "tra due ore e mezza", "fra mezz'ora", "tra 3 giorni/settimane"
_REL = re.compile(
    r"\b(?:tra|fra|entro|dopo)\s+(?P<n>\d{1,3})\s*(?P<unit>minut[oi]|min|or[ae]|h|giorn[oi]|settiman[ae]|mes[ei])"
    r"(?:\s+e\s+(?P<n2>\d{1,2})(?:\s*min(?:uti)?)?)?\b",
    re.IGNORECASE,
)
# --- clock: "alle 9", "alle 9 e 30", "alle 9:30", "alle ore 15", "per le 10", "a mezzogiorno", "alle 7 di sera", "alle 9 meno 15"
_CLOCK = re.compile(
    r"\b(?:alle|all['’]|per\s+le|verso\s+le|a|ore)\s*(?:ore\s+)?(?:(?P<noon>mezzogiorno)|(?P<midnight>mezzanotte)|(?P<una>l['’]una)|"
    r"(?P<h>\d{1,2})(?:[:.,](?P<m>\d{2}))?(?:\s+e\s+(?P<mm>\d{1,2}))?(?:\s+meno\s+(?P<less>\d{1,2}))?)"
    r"(?:\s+(?:di|del|della)\s+(?P<pod>mattina|mattino|pomeriggio|sera|notte))?\b",
    re.IGNORECASE,
)
# --- day words
_DAY = re.compile(
    r"\b(?:(?P<today>oggi|stamattina|stamane|stasera|stanotte|questa\s+(?:mattina|sera|notte)|questo\s+pomeriggio)|"
    r"(?P<tomorrow>domani)|(?P<after>dopodomani|dopo\s+domani)|"
    r"(?P<nextweek>(?:la\s+)?(?:settimana\s+prossima|prossima\s+settimana))|"
    r"(?P<wd>luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica)(?:\s+(?P<wdnext>prossim[oa]))?|"
    r"(?:il|l['’]|al)\s*(?P<dom>\d{1,2})(?:\s+(?P<mon>gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre))?(?:\s+(?P<year>20\d{2}))?(?!\s*[:.]\d)|"
    r"(?P<dom2>\d{1,2})/(?P<mon2>\d{1,2})(?:/(?P<year2>20\d{2}|\d{2}))?)\b",
    re.IGNORECASE,
)
# bare part of day, possibly after a day word: "domani pomeriggio", "in serata", "a pranzo"
_POD = re.compile(r"\b(?:(?:di|in|al|a|nel|nella|la|il)\s+)?(?P<pod>mattina|mattino|mattinata|pomeriggio|sera|serata|notte|pranzo)\b", re.IGNORECASE)
_TODAY_POD = {"stasera": "sera", "stanotte": "notte", "stamattina": "mattina", "stamane": "mattina"}


@dataclass
class When:
    at: datetime
    rest: str
    explicit_time: bool
    matched: str


def _words_to_digits(text: str) -> str:
    for pattern, repl in _IDIOMS:
        text = pattern.sub(repl, text)
    return _WORDS_RE.sub(lambda m: str(_NUM_WORDS[m.group(1).lower()]), text)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"^[\s,.;:\-]+|[\s,.;:\-]+$", "", text)
    # dangling connectives left behind by a removed time expression
    text = re.sub(r"^(?:e|che|di|a|per|poi|alle|il)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:e|che|di|a|per|alle|il|poi)$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _weekday(name: str) -> int:
    return _WEEKDAYS[name.lower()]


def parse(text: str, now: datetime | None = None) -> When | None:
    now = now or datetime.now()
    work = _words_to_digits(text)
    matched: list[str] = []

    # 1) relative durations win: "tra 20 minuti" is complete on its own.
    m = _REL.search(work)
    if m:
        n = int(m.group("n"))
        unit = m.group("unit").lower()
        extra = int(m.group("n2") or 0)
        if unit.startswith("min"):
            delta = timedelta(minutes=n)
        elif unit == "h" or unit.startswith("or"):
            delta = timedelta(hours=n, minutes=extra)
        elif unit.startswith("giorn"):
            delta = timedelta(days=n)
        elif unit.startswith("settiman"):
            delta = timedelta(weeks=n)
        else:
            delta = timedelta(days=30 * n)
        at = (now + delta).replace(second=0, microsecond=0)
        work = work[: m.start()] + " " + work[m.end():]
        if delta >= timedelta(days=1):
            # "tra 3 giorni alle 10" → honour the clock if present
            c = _CLOCK.search(work)
            hm = _clock_to_hm(c) if c else None
            if c and hm:
                at = at.replace(hour=hm[0], minute=hm[1])
                work = work[: c.start()] + " " + work[c.end():]
        return When(at=at, rest=_clean(work), explicit_time=True, matched=m.group(0))

    # 2) absolute: day word and/or clock and/or part of day.
    day: datetime | None = None
    pod: str | None = None
    d = _DAY.search(work)
    if d:
        matched.append(d.group(0))
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if d.group("today"):
            day = base
            word = d.group("today").lower()
            pod = _TODAY_POD.get(word)
            if "pomeriggio" in word:
                pod = "pomeriggio"
            elif word.startswith("questa"):
                pod = word.split()[-1]
        elif d.group("tomorrow"):
            day = base + timedelta(days=1)
        elif d.group("after"):
            day = base + timedelta(days=2)
        elif d.group("nextweek"):
            day = base + timedelta(days=7)
        elif d.group("wd"):
            ahead = (_weekday(d.group("wd")) - base.weekday()) % 7
            if d.group("wdnext") and ahead == 0:
                ahead = 7
            day = base + timedelta(days=ahead)
        elif d.group("dom") or d.group("dom2"):
            dom = int(d.group("dom") or d.group("dom2"))
            mon_s = d.group("mon")
            mon = _MONTHS[mon_s.lower()] if mon_s else (int(d.group("mon2")) if d.group("mon2") else now.month)
            year_s = d.group("year") or d.group("year2")
            year = int(year_s) if year_s and len(year_s) == 4 else (2000 + int(year_s) if year_s else now.year)
            try:
                day = base.replace(year=year, month=mon, day=dom)
            except ValueError:
                return None
            if day < base and not year_s:
                if mon_s or d.group("mon2"):
                    day = day.replace(year=day.year + 1)  # "il 15 marzo" said in October → next year
                else:
                    nm = 1 if now.month == 12 else now.month + 1  # "il 3" said on the 20th → next month
                    ny = now.year + 1 if now.month == 12 else now.year
                    try:
                        day = base.replace(year=ny, month=nm, day=dom)
                    except ValueError:
                        return None
        work = work[: d.start()] + " " + work[d.end():]

    c = _CLOCK.search(work)
    hm: tuple[int, int] | None = None
    if c:
        hm = _clock_to_hm(c)
        if hm:
            matched.append(c.group(0))
            work = work[: c.start()] + " " + work[c.end():]
            if c.group("pod"):
                pod = c.group("pod").lower()

    if hm is None:
        p = _POD.search(work)
        if p and (day is not None or pod is None):
            pod = p.group("pod").lower()
            matched.append(p.group(0))
            work = work[: p.start()] + " " + work[p.end():]

    if day is None and hm is None and pod is None:
        return None

    if hm is not None:
        h, mi = hm
        if pod in ("sera", "serata", "notte", "pomeriggio") and h < 12:
            h += 12
        explicit = True
    elif pod is not None:
        key = {"mattinata": "mattina", "serata": "sera"}.get(pod, pod)
        h, mi = _PART_OF_DAY.get(key, 9), 0
        explicit = False
    else:
        h, mi = 9, 0
        explicit = False

    if day is None:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    at = day.replace(hour=h, minute=mi)
    if at <= now:
        if d is None:
            at += timedelta(days=1)  # "alle 9" said at 10:00 → tomorrow 9:00
        elif d.group("wd") and not d.group("wdnext"):
            at += timedelta(days=7)  # "domenica sera" said on Sunday night → next Sunday
    return When(at=at, rest=_clean(work), explicit_time=explicit, matched=" ".join(matched))


def _clock_to_hm(c: re.Match[str]) -> tuple[int, int] | None:
    if c.group("noon"):
        return 12, 0
    if c.group("midnight"):
        return 0, 0
    if c.group("una"):
        return 13, 0
    h = int(c.group("h"))
    if h > 24:
        return None
    mi = int(c.group("m") or c.group("mm") or 0)
    if c.group("less"):
        h, mi = h - 1, 60 - int(c.group("less"))
    if h == 24:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def describe(at: datetime, now: datetime | None = None) -> str:
    """Spoken-friendly Italian: 'oggi alle 15:30', 'domani alle 9:00', 'lunedì 6 ottobre alle 10:00'."""
    now = now or datetime.now()
    today = now.date()
    d = at.date()
    clock = f"alle {at.hour}:{at.minute:02d}"
    if d == today:
        return f"oggi {clock}"
    if d == today + timedelta(days=1):
        return f"domani {clock}"
    if d == today + timedelta(days=2):
        return f"dopodomani {clock}"
    day = f"{_WEEKDAY_NAMES[at.weekday()]} {at.day} {_MONTH_NAMES[at.month]}"
    if at.year != now.year:
        day += f" {at.year}"
    return f"{day} {clock}"
