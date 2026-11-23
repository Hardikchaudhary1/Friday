"""Finds a date/time in a spoken sentence and splits it from the rest:
"call mom tomorrow at 5 pm" -> ("call mom", 2026-11-24 17:00, has_time=True)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_MONTHS = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
_BARE_MONTH = re.compile(rf"^(on |in |by |to )?{_MONTHS}$", re.I)
_BARE_WEEKDAY = re.compile(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$", re.I)
_TIME_HINT = re.compile(
    r"\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}|\bnoon\b|\bmidnight\b|o'?clock|"
    r"\bin \d+|\bin (a|an|half an) (minute|hour)|minutes?|hours?|morning|afternoon|evening|tonight|\bnow\b", re.I)
_AT_HOUR = re.compile(r"\b(at|by)\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(am|pm|:|th|st|nd|rd))", re.I)
_FILLER = re.compile(r"\s+\b(on|at|by|for|in|this|the|of)$", re.I)


@dataclass
class ParsedTime:
    text: str              # the sentence with the time phrase removed
    when: datetime | None
    has_time: bool         # False when only a date was said ("on Friday")


def _clean(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", text).strip(" ,.")
    while _FILLER.search(text):
        text = _FILLER.sub("", text).strip(" ,.")
    return text


def _next_hour(now: datetime, hour: int, minute: int) -> datetime:
    """'at 7' means the next 7 o'clock: 7 pm if 7 am has already passed today."""
    candidates = [hour] if hour >= 12 else [hour, hour + 12]
    for day in (0, 1):
        for h in candidates:
            t = (now + timedelta(days=day)).replace(hour=h % 24, minute=minute, second=0, microsecond=0)
            if t > now:
                return t
    return now + timedelta(days=1)


def parse(sentence: str, now: datetime | None = None, default_hour: int = 9) -> ParsedTime:
    from dateparser.search import search_dates

    now = now or datetime.now()
    settings = {"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now, "RETURN_AS_TIMEZONE_AWARE": False}

    # A bare hour ("at 7") confuses dateparser (it may read 7 as July), so take it out first.
    bare_hour = None
    at = _AT_HOUR.search(sentence)
    if at and int(at.group(2)) <= 23:
        bare_hour = (int(at.group(2)), int(at.group(3) or 0))
        sentence = sentence.replace(at.group(0), " ")

    found = search_dates(sentence, languages=["en"], settings=settings) or []
    found = [(s, d) for s, d in found if not _BARE_MONTH.match(s.strip())]
    if len(found) > 1:
        # "remind me to tell Friday ... in 10 minutes": a lone weekday word next to a
        # proper time phrase is part of the sentence, not the date.
        found = [(s, d) for s, d in found if not _BARE_WEEKDAY.match(s.strip())] or found

    if found:
        # Several fragments ("tomorrow", "at 5 pm") can come back separately; combine them.
        phrase = " ".join(s for s, _ in found)
        when = found[-1][1] if len(found) == 1 else (search_dates(phrase, languages=["en"], settings=settings) or found)[-1][1]
        has_time = bool(_TIME_HINT.search(phrase))
        rest = sentence
        for s, _ in found:
            rest = rest.replace(s, " ")
        if bare_hour and not has_time:
            h, m = bare_hour
            if h <= 12:
                h = h + 12 if h < 8 else h  # on a given day, "at 7" means 7 pm and "at 9" means 9 am
            when, has_time = when.replace(hour=h % 24, minute=m), True
        if not has_time:
            when = when.replace(hour=default_hour, minute=0)
        return ParsedTime(_clean(rest), when.replace(second=0, microsecond=0), has_time)

    if bare_hour:
        return ParsedTime(_clean(sentence), _next_hour(now, *bare_hour), True)
    return ParsedTime(_clean(sentence), None, False)
