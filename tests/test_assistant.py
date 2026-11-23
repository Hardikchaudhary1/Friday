from datetime import date, datetime, time, timedelta

import pytest

from friday.apps import App, AppIndex, open_target, resolve_website
from friday.assistant import Assistant
from friday.config import Config
from friday.schedule import ScheduleStore
from friday.timeparse import parse

NOW = datetime(2026, 11, 23, 14, 30)  # Monday afternoon


class FakeIndex(AppIndex):
    def __init__(self, names):
        super().__init__(cache_path=None)
        self._apps = [App(n, f"id.{n}") for n in names]


class NoLLM:
    name = "none"

    def available(self):
        return False


@pytest.fixture
def friday(tmp_path):
    cfg = Config({"user_name": "Tester", "data_dir": str(tmp_path)})
    apps = FakeIndex(["Brave", "Calculator", "Spotify", "Visual Studio Code", "Battlefield™ V", "Camera"])
    clock = {"now": NOW}
    a = Assistant(cfg, app_index=apps, now_fn=lambda: clock["now"], dry_run=True, llm_backends=[NoLLM()])
    a.clock = clock
    return a


# ---- time parsing --------------------------------------------------------------

@pytest.mark.parametrize("text, rest, when, has_time", [
    ("call mom tomorrow at 5 pm", "call mom", datetime(2026, 11, 24, 17, 0), True),
    ("buy milk at 7", "buy milk", datetime(2026, 11, 23, 19, 0), True),
    ("team sync on friday at 7", "team sync", datetime(2026, 11, 27, 19, 0), True),
    ("pay rent on 1st December", "pay rent", datetime(2026, 12, 1, 9, 0), False),
    ("stretch in 20 minutes", "stretch", datetime(2026, 11, 23, 14, 50), True),
    ("send email to may tomorrow", "send email to may", datetime(2026, 11, 24, 9, 0), False),
    ("call dad", "call dad", None, False),
    ("check that Friday reminders work in 1 minute", "check that Friday reminders work",
     datetime(2026, 11, 23, 14, 31), True),
])
def test_parse(text, rest, when, has_time):
    p = parse(text, NOW)
    assert (p.text, p.when, p.has_time) == (rest, when, has_time)


# ---- reminders -------------------------------------------------------------------

def test_reminder_with_time(friday):
    r = friday.handle("Hey Friday, remind me to call mom tomorrow at 5 pm")
    assert r.text == "Okay, I'll remind you to call mom tomorrow at 5 PM."
    due = friday.schedule.pending_reminders()
    assert [(x.text, x.due_at) for x in due] == [("call mom", datetime(2026, 11, 24, 17, 0))]


def test_reminder_asks_for_missing_time(friday):
    assert friday.handle("remind me to water the plants").text == "When should I remind you to water the plants?"
    r = friday.handle("in 2 hours")
    assert "water the plants today at 4:30 PM" in r.text


def test_reminder_in_the_past_is_refused(friday):
    assert "already passed" in friday.handle("remind me to eat lunch today at 1 pm").text
    assert friday.schedule.pending_reminders() == []


def test_due_reminders_fire_once(friday):
    friday.handle("remind me to stretch in 10 minutes")
    store = friday.schedule
    assert store.due_reminders(NOW) == []
    due = store.due_reminders(NOW + timedelta(minutes=10))
    assert [r.text for r in due] == ["stretch"]
    store.mark_fired(due[0].id)
    assert store.due_reminders(NOW + timedelta(hours=1)) == []


def test_list_and_cancel_reminders(friday):
    friday.handle("remind me to call the bank tomorrow at 11 am")
    friday.handle("remind me to book flights on friday")
    assert friday.handle("what are my reminders").text.startswith("You have 2 reminders.")
    assert friday.handle("cancel the reminder about the bank").text == "Cancelled the reminder: call the bank."
    assert friday.handle("cancel the last reminder").text == "Cancelled the reminder: book flights."
    assert friday.handle("list my reminders").text == "You have no upcoming reminders."


# ---- events ----------------------------------------------------------------------

def test_event_with_time_gets_two_reminders(friday):
    r = friday.handle("add dentist appointment on 5th December at 4pm")
    assert r.text == ("Added dentist appointment on Saturday 5 December at 4 PM. "
                      "I'll remind you that morning and 30 minutes before.")
    reminders = friday.schedule.pending_reminders()
    assert [x.due_at for x in reminders] == [datetime(2026, 12, 5, 9, 0), datetime(2026, 12, 5, 15, 30)]
    assert [x.text for x in reminders] == ["Today: dentist appointment at 4 PM", "In 30 minutes: dentist appointment"]


# ---- speech clean-up and wake word ---------------------------------------------------

@pytest.mark.parametrize("heard, cleaned", [
    ("Friday, open, Spotify.", "open Spotify"),
    ("Hey Friday, remind me to call mom tomorrow at 5 p.m.", "remind me to call mom tomorrow at 5 pm"),
    ("Okay Friday, what are my tasks?", "what are my tasks"),
])
def test_normalise_speech(heard, cleaned):
    from friday.assistant import normalise_speech
    assert normalise_speech(heard) == cleaned


@pytest.mark.parametrize("transcript, ok", [
    ("Friday, open Spotify.", True), ("Okay Friday, what are my tasks?", True),
    ("so um Friday open notes", True), ("Fri day, brief me", True),
    ("What is the weather like in London today?", False),
    ("I'm free on Monday, why don't we meet then?", False),
    ("Let's meet next week on Friday afternoon if that works for everyone", False),
])
def test_wake_word_confirmed(transcript, ok):
    from friday.listen import wake_word_confirmed
    assert wake_word_confirmed(transcript) is ok


def test_all_day_event_and_calendar_phrase(friday):
    r = friday.handle("put mom's birthday on 12 January in my calendar")
    assert r.text.startswith("Added mom's birthday on Tuesday 12 January.")
    assert friday.schedule.events_between(date(2027, 1, 12), date(2027, 1, 12))[0].at is None


def test_event_asks_for_date(friday):
    assert friday.handle("schedule a meeting called project review").text == "What date is project review on?"
    assert friday.handle("next wednesday at 3 pm").text.startswith("Added project review on Wednesday")


def test_schedule_queries(friday):
    friday.handle("add team standup tomorrow at 10 am")
    assert friday.handle("what's on my schedule today").text == "Nothing on your calendar today."
    r = friday.handle("what do I have tomorrow")
    assert r.text == "You have 1 event tomorrow. team standup tomorrow at 10 AM."
    assert friday.handle("cancel the meeting team standup").text.startswith("Cancelled team standup")


def test_event_today_skips_past_morning_reminder():
    store_now = datetime(2026, 11, 23, 14, 0)
    import tempfile, pathlib
    store = ScheduleStore(pathlib.Path(tempfile.mkdtemp()) / "s.db")
    _, reminders = store.add_event("Call", date(2026, 11, 23), time(14, 10), now=store_now)
    assert [r.text for r in reminders] == ["Starting now: Call"]


# ---- tasks -------------------------------------------------------------------------

def test_tasks_by_voice(friday):
    assert friday.handle("add task pay rent due friday").text == "Added task: pay rent, due Fri 27 Nov."
    assert friday.handle("add buy groceries to my to-do list").text == "Added task: buy groceries."
    assert friday.handle("what are my tasks").text.startswith("You have 2 open tasks.")
    assert friday.handle("mark pay rent as done").text == "Marked done: pay rent."
    assert friday.handle("what are my tasks").text == "You have 1 open task. buy groceries."


# ---- apps and web --------------------------------------------------------------------

@pytest.mark.parametrize("said, reply", [
    ("open spotify", "Opening Spotify."),
    ("launch vs code", "Opening Visual Studio Code."),
    ("open the camera app", "Opening Camera."),
    ("open battlefield", "Opening Battlefield V."),
    ("open youtube", "Opening youtube."),
    ("open github dot com", "Opening github dot com."),
    ("open xyzzy", "I couldn't find an app called xyzzy. Which app should I open?"),
])
def test_open(friday, said, reply):
    assert friday.handle(said).text == reply


def test_misheard_app_asks_and_takes_the_answer(friday):
    assert friday.handle("Friday, open, 45.").text == "I couldn't find an app called 45. Which app should I open?"
    assert friday.handle("Spotify").text == "Opening Spotify."


def test_login_command(friday):
    assert friday.handle("/login").text.startswith("I've opened a window to sign in to Claude")


def test_hotwords_prefer_short_names():
    from friday.listen import build_hotwords
    hw = build_hotwords(["Spotify", "Battlefield™ V", "Visual Studio Code", "A Very Long Application Name Here", "spotify"])
    assert hw == "Spotify, Battlefield V, Visual Studio Code"


def test_open_suggests_close_match_and_confirms(friday):
    r = friday.handle("open calculater app")
    assert r.text in ("Opening Calculator.", "I couldn't find calculater. Did you mean Calculator?")
    if "Did you mean" in r.text:
        assert friday.handle("yes").text == "Opening Calculator."


def test_websites():
    assert resolve_website("YouTube") == "https://www.youtube.com"
    assert resolve_website("example dot org") == "https://example.org"
    assert resolve_website("spotify") is None


def test_search(friday):
    assert friday.handle("search for brent crude price").text == "Searching the web for brent crude price."


# ---- media, volume, sites and windows -------------------------------------------------

@pytest.mark.parametrize("said, key", [
    ("pause", "play_pause"), ("pause the music", "play_pause"), ("resume", "play_pause"),
    ("play", "play_pause"), ("stop the music", "play_pause"), ("play some music", "play_pause"),
    ("next song", "next"), ("skip", "next"), ("skip this one", "next"),
    ("previous track", "previous"), ("go back", "previous"),
])
def test_media_keys(friday, said, key):
    r = friday.handle(f"Friday, {said}")
    assert r.text == "" and r.data == {"media": key}


def test_bare_stop_stops_friday_not_music(friday):
    assert friday.handle("stop").action == "stop"


@pytest.mark.parametrize("said, reply", [
    ("set volume to 40", "Volume 40 percent."), ("volume 75 percent", "Volume 75 percent."),
    ("turn it up", "Volume up."), ("quieter please", "Volume down."), ("lower the volume a little", "Volume down."),
    ("unmute", "Sound on."), ("mute", ""),
])
def test_volume_commands(friday, said, reply):
    assert friday.handle(said).text == reply


@pytest.mark.parametrize("said, reply", [
    ("play lofi beats on spotify", "Searching Spotify for lofi beats."),
    ("play the song blinding lights on youtube", "Playing blinding lights on YouTube."),
    ("play interstellar soundtrack", "Playing interstellar soundtrack on YouTube."),
    ("search for mechanical keyboards on amazon", "Searching Amazon for mechanical keyboards."),
    ("look up fastapi on github", "Searching Github for fastapi."),
    ("search youtube for python tutorials", "Searching Youtube for python tutorials."),
])
def test_play_and_site_search(friday, said, reply):
    assert friday.handle(said).text == reply


def test_close_and_switch(friday):
    friday.window_lister = lambda: [
        {"pid": 1, "name": "Spotify", "title": "Spotify Premium"},
        {"pid": 2, "name": "brave", "title": "YouTube - Brave"},
        {"pid": 3, "name": "explorer", "title": "Downloads"},
    ]
    assert friday.handle("close spotify").text == "Closing Spotify."
    assert friday.handle("switch to brave").text == "Switching to Brave."
    assert friday.handle("close explorer").text == "I won't close explorer by voice, to be safe."
    assert friday.handle("close notepad").text == "notepad doesn't seem to be open."
    assert friday.handle("close friday").text == "To close me, type quit in my window."
    assert friday.handle("switch to calculator").text == "Opening Calculator."  # not running: opens it


def test_site_search_urls():
    from friday.media import search_site
    assert search_site("amazon", "usb c hub", dry_run=True) == "https://www.amazon.com/s?k=usb+c+hub"
    assert search_site("maps", "coffee near me", dry_run=True) == "https://www.google.com/maps/search/coffee%20near%20me"


def test_generic_spotify_requests_play_instead_of_searching(friday):
    friday.window_lister = lambda: [{"pid": 1, "name": "Spotify", "title": "Spotify Premium"}]
    for said in ("play music on spotify", "play something on spotify", "play spotify", "play my playlist on spotify"):
        r = friday.handle(f"Friday, {said}.")
        assert r.data == {"media": "play_pause"}, said
    friday.window_lister = lambda: []
    assert friday.handle("play music on spotify").text == "Opening Spotify and starting your music."
    assert friday.handle("play blinding lights on spotify").text == "Searching Spotify for blinding lights."


@pytest.mark.parametrize("heard, key", [("Friday, late.", "play_pause"), ("Friday, paws.", "play_pause"),
                                         ("Friday, necks.", "next")])
def test_common_mishearings(friday, heard, key):
    assert friday.handle(heard).data == {"media": key}


# ---- clearing in bulk ---------------------------------------------------------------

def test_remove_my_schedules_asks_then_clears(friday):
    friday.handle("add standup tomorrow at 10 am")
    friday.handle("add dentist on 5th December at 4pm")
    friday.handle("remind me to call the bank tomorrow at 11 am")
    friday.handle("add task pay rent")
    assert friday.handle("Friday, remove my schedules").text == "Delete 2 events? Say yes to confirm."
    assert friday.handle("yes").text == "Done. Removed 2 items."
    assert friday.schedule.events_between(date(2026, 1, 1), date(2027, 12, 31)) == []
    # The standalone reminder and the task are untouched; event reminders went with their events.
    assert [r.text for r in friday.schedule.pending_reminders()] == ["call the bank"]
    assert friday.handle("delete everything").text == "Delete 1 reminder and 1 task? Say yes to confirm."
    assert friday.handle("no").text == "Okay, I've left everything as it is."
    assert len(friday.tasks.open_tasks()) == 1


def test_clear_for_a_day(friday):
    friday.handle("add standup tomorrow at 10 am")
    friday.handle("add review on friday at 3 pm")
    assert friday.handle("clear my calendar for tomorrow").text == "Delete 1 event for tomorrow? Say yes to confirm."
    friday.handle("yes")
    assert [e.title for e in friday.schedule.events_between(date(2026, 1, 1), date(2027, 12, 31))] == ["review"]
    assert friday.handle("delete all my reminders").text == "There's nothing to remove."


def test_wake_word_alone():
    from friday.listen import only_wake_word
    assert only_wake_word("Friday,") and only_wake_word("Hey Friday.") and only_wake_word(" friday ")
    assert not only_wake_word("Friday, pause")


# ---- streamed briefing --------------------------------------------------------------

def test_streamed_briefing_hands_over_paragraphs(tmp_path, monkeypatch):
    from friday import briefing as B

    class FakeStreamer:
        name = "fake"

        def available(self):
            return True

        def stream(self, system, prompt):
            text = "Good morning.\n\nMarkets fell. Oil rose.\n\nThat's all."
            for i in range(0, len(text), 7):   # arrives in small pieces, like a real stream
                yield text[i:i + 7]

    monkeypatch.setattr(B, "build_backends", lambda cfg: [FakeStreamer()])
    cfg = Config({"user_name": "T", "data_dir": str(tmp_path)})
    data = B.BriefingData(today=NOW.date(), headlines=[], quotes=[], tasks=[])
    parts = []
    result = B.write_briefing_streaming(cfg, data, on_part=parts.append)
    assert parts == ["Good morning.", "Markets fell. Oil rose.", "That's all."]
    assert result.backend == "fake" and result.text.startswith("Good morning.")


# ---- misc ----------------------------------------------------------------------------

def test_small_talk_and_fallback(friday):
    assert friday.handle("Friday").text == "Yes?"
    assert friday.handle("what time is it").text == "It's 2:30 PM."
    assert friday.handle("brief me").action == "briefing"
    assert friday.handle("stop").action == "stop"
    assert "Sign in to the Claude CLI" in friday.handle("why is the sky blue").text
