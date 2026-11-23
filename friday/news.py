"""Fetches RSS headlines, removes duplicates, and ranks them by likely market impact."""
from __future__ import annotations

import calendar
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html import unescape

import feedparser

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FridayBriefing/0.1"

# Keyword -> weight. A headline's score is the sum of the weights it matches,
# plus a bonus for being recent and for appearing in several sources.
IMPACT_KEYWORDS: dict[str, float] = {
    # Geopolitics and conflict
    r"\bsanction": 3, r"\btariff": 3, r"\btrade war": 3, r"\bwar\b": 2.5, r"\binvasion": 3,
    r"\bmissile": 2, r"\bstrike[s]?\b": 1.5, r"\bceasefire": 2.5, r"\bnuclear": 2,
    r"\bmilitary": 1.5, r"\bblockade": 3, r"\bembargo": 3, r"\bcoup\b": 2.5,
    r"\belection": 1.5, r"\bsummit": 1.5, r"\bexport control": 3,
    r"\btaiwan": 2, r"\bchina\b": 1.5, r"\brussia": 1.5, r"\bukraine": 1.5, r"\biran": 2,
    r"\bisrael": 1.5, r"\bmiddle east": 1.5, r"\bred sea": 2.5, r"\bstrait of hormuz": 3,
    r"\bnorth korea": 1.5, r"\bnato\b": 1.5, r"\bopec": 3,
    # Macro and central banks
    r"\bfed\b": 3, r"\bfederal reserve": 3, r"\becb\b": 2.5, r"\bbank of japan": 2.5, r"\bboj\b": 2.5,
    r"\bpboc\b": 2, r"\bbank of england": 2, r"\brate cut": 3, r"\brate hike": 3,
    r"\binterest rate": 2.5, r"\binflation": 2.5, r"\bcpi\b": 2.5, r"\bjobs report": 2.5,
    r"\bpayrolls": 2.5, r"\bgdp\b": 2, r"\brecession": 2.5, r"\bdebt ceiling": 2.5,
    r"\bshutdown": 1.5, r"\bbond yield": 2, r"\btreasur": 2, r"\bdefault": 2,
    # Markets and commodities
    r"\boil\b": 2, r"\bcrude": 2, r"\bbrent": 2, r"\bgold\b": 1.5, r"\bcopper": 1.5,
    r"\bnatural gas": 1.5, r"\bsemiconductor": 2, r"\bchip": 1.5, r"\bstocks?\b": 1,
    r"\bmarkets?\b": 1, r"\bselloff|sell-off": 2, r"\brally": 1, r"\bdollar": 1.5,
    r"\byuan|yen\b|euro\b": 1.5, r"\bbitcoin|crypto": 1, r"\bearnings": 1,
    r"\bsupply chain": 2, r"\bshipping": 1.5,
}
_COMPILED = [(re.compile(p, re.IGNORECASE), w) for p, w in IMPACT_KEYWORDS.items()]
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Headline:
    title: str
    source: str
    link: str = ""
    summary: str = ""
    published_ts: float | None = None
    score: float = 0.0
    also_in: list[str] = field(default_factory=list)

    @property
    def age_hours(self) -> float | None:
        if self.published_ts is None:
            return None
        return max(0.0, (time.time() - self.published_ts) / 3600)


def clean_text(text: str) -> str:
    text = unescape(_TAG_RE.sub(" ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _normalise_title(title: str) -> str:
    # Google News appends " - Publisher"; drop it so duplicates match.
    title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def impact_score(headline: Headline, now: float | None = None) -> float:
    text = f"{headline.title} {headline.summary}"
    score = sum(w for rx, w in _COMPILED if rx.search(text))
    age = headline.age_hours if now is None else (
        None if headline.published_ts is None else max(0.0, (now - headline.published_ts) / 3600)
    )
    if age is not None:
        if age <= 6:
            score += 2
        elif age <= 18:
            score += 1
        elif age > 48:
            score -= 3
    score += 1.5 * len(headline.also_in)  # covered by several outlets
    return score


def dedupe(headlines: list[Headline], threshold: float = 0.72) -> list[Headline]:
    kept: list[tuple[str, Headline]] = []
    for h in headlines:
        norm = _normalise_title(h.title)
        if not norm:
            continue
        for k_norm, k in kept:
            if SequenceMatcher(None, norm, k_norm).ratio() >= threshold:
                if h.source != k.source and h.source not in k.also_in:
                    k.also_in.append(h.source)
                break
        else:
            kept.append((norm, h))
    return [h for _, h in kept]


def select_top(headlines: list[Headline], n: int, per_source: int = 5) -> list[Headline]:
    """Takes the top n ranked headlines, at most per_source from any one feed,
    so a single narrow feed cannot crowd out everything else."""
    counts: dict[str, int] = {}
    picked: list[Headline] = []
    for h in headlines:
        if counts.get(h.source, 0) >= per_source:
            continue
        counts[h.source] = counts.get(h.source, 0) + 1
        picked.append(h)
        if len(picked) >= n:
            break
    return picked


def _parse_feed(name: str, url: str, per_feed: int) -> tuple[list[Headline], str | None]:
    try:
        parsed = feedparser.parse(url, agent=USER_AGENT, request_headers={"Accept": "*/*"})
    except Exception as exc:  # network errors, bad XML
        return [], f"{name}: {exc}"
    if not parsed.entries:
        status = getattr(parsed, "status", "?")
        return [], f"{name}: no entries (HTTP {status})"
    out = []
    for entry in parsed.entries[:per_feed]:
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                ts = float(calendar.timegm(entry[key]))
                break
        out.append(Headline(
            title=clean_text(entry.get("title", "")),
            source=name,
            link=entry.get("link", ""),
            summary=clean_text(entry.get("summary", ""))[:300],
            published_ts=ts,
        ))
    return out, None


def fetch_headlines(feeds: list[dict], per_feed: int = 25, max_age_hours: float = 36) -> tuple[list[Headline], list[str]]:
    """Returns (ranked headlines, list of feed errors)."""
    all_items: list[Headline] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda f: _parse_feed(f["name"], f["url"], per_feed), feeds)
        for items, err in results:
            all_items.extend(items)
            if err:
                errors.append(err)

    fresh = [h for h in all_items if h.age_hours is None or h.age_hours <= max_age_hours]
    # Newest first so the freshest version of a story is the one kept.
    fresh.sort(key=lambda h: h.published_ts or 0, reverse=True)
    unique = dedupe(fresh)
    for h in unique:
        h.score = impact_score(h)
    unique.sort(key=lambda h: h.score, reverse=True)
    return unique, errors
