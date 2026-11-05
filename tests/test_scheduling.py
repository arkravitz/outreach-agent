from datetime import datetime, timedelta
import pytest
from pipeline.calendar import Scheduler
from pipeline.outbound import Blocked, business_days
from test_replies import sent, event
from zoneinfo import ZoneInfo


def future_slot() -> datetime:
    """The next weekday at 10:00 Pacific, so booking tests never go stale."""
    day = datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.replace(hour=10, minute=0, second=0, microsecond=0)


class Calendar:
    def __init__(self):
        self.events = {}
        self.busy = []
        self.availability_calls = 0

    def get_event(self, calendar_id, event_id):
        return self.events.get(event_id)

    def freebusy(self, calendar_id, start, end):
        self.availability_calls += 1
        return self.busy

    def insert(self, calendar_id, body):
        self.events[body["id"]] = body
        return body


def test_business_days_skip_weekend_and_preserve_local_time_across_dst():
    start = datetime.fromisoformat("2026-10-30T16:00:00+00:00")
    assert business_days(start, 1).isoformat() == "2026-11-02T17:00:00+00:00"
    assert business_days(datetime.fromisoformat("2026-10-16T16:00:00+00:00"), 4).weekday() == 3


def test_schedule_correct_timezone_event_and_no_duplicate_booking(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    app.ingest(event(contact, "I am interested"))
    app.classify("incoming-1")
    calendar = Calendar()
    scheduler = Scheduler(app, calendar)
    start = future_slot()
    meeting = scheduler.book(contact["id"], start, approved=True)
    assert meeting["start"]["dateTime"] == start.isoformat()
    assert meeting["attendees"] == [{"email": seed.email}]
    assert scheduler.book(contact["id"], start, approved=True)["id"] == meeting["id"]
    assert len(calendar.events) == 1
    assert calendar.availability_calls == 1
    assert app.store.one("select state from contacts")["state"] == "MEETING_BOOKED"


def test_calendar_rechecks_fresh_busy_time_before_booking(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    calendar = Calendar()
    scheduler = Scheduler(app, calendar)
    start = future_slot()
    calendar.busy = [(start, start + timedelta(hours=1))]
    with pytest.raises(Blocked, match="available"):
        scheduler.book(contact["id"], start, approved=True)
    assert not calendar.events


def test_calendar_requires_confirmation_and_timezone(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    scheduler = Scheduler(app, Calendar())
    with pytest.raises(Blocked, match="approval"):
        scheduler.book(contact["id"], future_slot())
    with pytest.raises(ValueError, match="timezone"):
        scheduler.book(contact["id"], future_slot().replace(tzinfo=None), approved=True)


def test_calendar_dry_run_and_pause_block_invitations(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    scheduler = Scheduler(app, Calendar())
    app.store.execute("update settings set value='true' where key='paused'")
    with pytest.raises(Blocked, match="paused"):
        scheduler.book(contact["id"], future_slot(), approved=True)


def test_calendar_rejects_invalid_state_before_external_invitation(setup, seed):
    app, _, _, _ = setup
    contact = app.prepare("automation", seed)
    calendar = Calendar()
    with pytest.raises(Blocked, match="state"):
        Scheduler(app, calendar).book(
            contact["id"], future_slot(), approved=True
        )
    assert not calendar.events


def test_booking_refreshes_state_after_acquiring_gate(setup, seed, monkeypatch):
    app, _, contact, _ = sent(setup, seed)
    calendar = Calendar()
    original = app.store.one
    changed = []

    def concurrent_decline(sql, params=()):
        row = original(sql, params)
        if sql == "select * from contacts where id=?" and not changed:
            changed.append(True)
            app.store.execute("update contacts set state='LOST' where id=?", (contact["id"],))
        return row

    monkeypatch.setattr(app.store, "one", concurrent_decline)
    with pytest.raises(Blocked):
        Scheduler(app, calendar).book(
            contact["id"], future_slot(), approved=True
        )
    assert not calendar.events
