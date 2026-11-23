import time
from datetime import date, datetime

from friday.briefing import BriefingData, build_prompt, template_briefing
from friday.config import Config
from friday.markets import Quote, biggest_movers, format_quote
from friday.news import Headline, dedupe, impact_score, select_top
from friday.tasks import TaskStore, parse_due

CFG = Config({"user_name": "Tester", "briefing": {"max_headlines_for_llm": 10, "target_words": 300}})
MONDAY = date(2026, 11, 23)


# ---- tasks -------------------------------------------------------------------

def test_parse_due_keywords():
    assert parse_due("today", MONDAY) == MONDAY
    assert parse_due("tomorrow", MONDAY) == date(2026, 11, 24)
    assert parse_due("fri", MONDAY) == date(2026, 11, 27)
    assert parse_due("monday", MONDAY) == date(2026, 11, 30)  # next week, not today
    assert parse_due("2026-12-01", MONDAY) == date(2026, 12, 1)
    assert parse_due(None) is None


def test_task_store_today_view(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    overdue = store.add("Pay card bill", date(2026, 11, 20))
    store.add("Gym", MONDAY)
    store.add("Read paper")
    store.add("Dentist", date(2026, 11, 30))
    today = [t.text for t in store.for_today(MONDAY)]
    assert today == ["Pay card bill", "Gym", "Read paper"]
    assert overdue.due_label(MONDAY).startswith("overdue")
    assert store.complete(overdue.id)
    assert not store.complete(overdue.id)  # already done
    assert "Pay card bill" not in [t.text for t in store.for_today(MONDAY)]


# ---- news --------------------------------------------------------------------

def test_dedupe_merges_same_story_across_sources():
    items = [
        Headline("Oil jumps as OPEC agrees surprise output cut", "A"),
        Headline("Oil jumps as OPEC agrees surprise output cut - Reuters", "B"),
        Headline("Local football club wins cup", "C"),
    ]
    out = dedupe(items)
    assert len(out) == 2
    assert out[0].also_in == ["B"]


def test_impact_score_prefers_market_news():
    now = time.time()
    macro = Headline("Fed signals rate hike as inflation climbs; oil and dollar rally", "X", published_ts=now)
    fluff = Headline("Celebrity opens new restaurant", "X", published_ts=now)
    stale = Headline("Fed signals rate hike as inflation climbs; oil and dollar rally", "X", published_ts=now - 72 * 3600)
    assert impact_score(macro) > impact_score(fluff)
    assert impact_score(macro) > impact_score(stale)


def test_select_top_caps_each_source():
    items = [Headline(f"story {i}", "Loud") for i in range(10)] + [Headline("other", "Quiet")]
    picked = select_top(items, 6, per_source=3)
    assert [h.source for h in picked] == ["Loud"] * 3 + ["Quiet"]


# ---- markets -----------------------------------------------------------------

def test_format_quote_and_movers():
    spx = Quote("^GSPC", "S&P 500", "Equities", 6910.0, -3.7, "2026-11-20", 7175.0)
    tnx = Quote("^TNX", "US 10Y yield", "Rates", 4.91, -2.77, "2026-11-20", 5.05)
    gold = Quote("GC=F", "Gold", "Commodities", 3615.0, 2.83)
    missing = Quote("EURUSD=X", "EUR/USD", "FX", None, None)
    assert format_quote(spx) == "S&P 500: 6,910 (-3.70%)"
    assert format_quote(tnx) == "US 10Y yield: 4.91% (-14 bp)"
    assert format_quote(missing) == "EUR/USD: unavailable"
    assert [q.name for q in biggest_movers([spx, tnx, gold, missing], 2)] == ["S&P 500", "Gold"]


# ---- briefing ----------------------------------------------------------------

def _data():
    return BriefingData(
        today=MONDAY,
        headlines=[Headline("Fed signals another rate hike", "CNBC", published_ts=time.time())],
        quotes=[Quote("BZ=F", "Brent crude", "Commodities", 89.39, -3.44, "2026-11-23", 92.57)],
        tasks=[],
    )


def test_prompt_contains_all_sections():
    prompt = build_prompt(CFG, _data(), now=datetime(2026, 11, 23, 8, 30))
    assert "morning briefing" in prompt
    assert "Brent crude: 89.39 (-3.44%)" in prompt
    assert "Fed signals another rate hike" in prompt
    assert "(no open tasks)" in prompt


def test_template_briefing_without_ai():
    text = template_briefing(CFG, _data(), now=datetime(2026, 11, 23, 19, 0))
    assert text.startswith("Good evening, Tester.")
    assert "Brent crude down 3.4 percent" in text
    assert "From " not in text
    assert "task list is clear" in text
