"""Fresh free/busy, explicit agreement and provider-idempotent calendar insertion."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .models import digest, now
from .outbound import Blocked

BASE = "https://www.googleapis.com/calendar/v3"


class GoogleCalendar:
    def __init__(self, client):
        self.client = client

    def get_event(self, calendar_id, event_id):
        from urllib.parse import quote

        response = self.client.get(f"{BASE}/calendars/{quote(calendar_id, safe='')}/events/{event_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def freebusy(self, calendar_id, start, end):
        response = self.client.post(
            BASE + "/freeBusy",
            json={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": calendar_id}]},
        )
        response.raise_for_status()
        calendar = response.json()["calendars"][calendar_id]
        if calendar.get("errors"):
            raise Blocked("Calendar availability could not be established")
        return [
            (
                datetime.fromisoformat(x["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(x["end"].replace("Z", "+00:00")),
            )
            for x in calendar.get("busy", [])
        ]

    def insert(self, calendar_id, body):
        from urllib.parse import quote

        response = self.client.post(
            f"{BASE}/calendars/{quote(calendar_id, safe='')}/events", params={"sendUpdates": "all"}, json=body
        )
        if response.status_code == 409:
            return self.get_event(calendar_id, body["id"])
        response.raise_for_status()
        return response.json()


class Scheduler:
    def __init__(self, app, calendar):
        self.app = app
        self.calendar = calendar

    def slots(self, campaign_id, start, days=7, limit=6):
        campaign = self.app.campaign(campaign_id)
        if not start.tzinfo:
            raise ValueError("Explicit timezone required")
        end = start + timedelta(days=days)
        busy = self.calendar.freebusy(campaign.calendar_id, start, end)
        zone = ZoneInfo(campaign.timezone)
        local = start.astimezone(zone)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        slots = []
        for offset in range(days):
            cursor = (day + timedelta(days=offset)).replace(hour=campaign.working_start)
            stop = cursor.replace(hour=0) + timedelta(hours=campaign.working_end)
            while cursor + timedelta(minutes=campaign.meeting_minutes) <= stop:
                finish = cursor + timedelta(minutes=campaign.meeting_minutes)
                if (
                    cursor.weekday() < 5
                    and cursor >= start
                    and not any(cursor < b and finish > a for a, b in busy)
                ):
                    slots.append({"start": cursor.isoformat(), "end": finish.isoformat()})
                    if len(slots) >= limit:
                        return slots
                cursor = finish
        return slots

    def book(self, contact_id, start, approved=False):
        if not approved:
            raise Blocked("Explicit user approval of the mutually agreed slot is required")
        if start.tzinfo is None:
            raise ValueError("Explicit timezone required")
        contact = self.app.store.one("select * from contacts where id=?", (contact_id,))
        if not contact:
            raise ValueError("Unknown contact")
        campaign = self.app.campaign(contact["campaign_id"])
        end = start + timedelta(minutes=campaign.meeting_minutes)
        event_id = "p" + digest(f"{contact_id}:{start.astimezone(now().tzinfo).isoformat()}")[:40]
        with self.app.store.tx() as db:
            contact = dict(db.execute("select * from contacts where id=?", (contact_id,)).fetchone())
            from .models import Campaign

            campaign = Campaign.model_validate_json(
                db.execute("select config from campaigns where id=?", (contact["campaign_id"],)).fetchone()[0]
            )
            end = start + timedelta(minutes=campaign.meeting_minutes)
            paused = db.execute("select value from settings where key='paused'").fetchone()[0] == "true"
            campaign_row = db.execute("select paused from campaigns where id=?", (campaign.id,)).fetchone()
            if paused or campaign_row["paused"]:
                raise Blocked("Calendar actions paused")
            if campaign.mode == "DRY_RUN":
                raise Blocked("DRY_RUN cannot create invitations")
            if (
                contact["invalid"]
                or contact["state"] in {"SUPPRESSED", "LOST", "BOUNCED"}
                or db.execute(
                    "select 1 from suppressions where email=? or company_id=?",
                    (contact["email"], contact["company_id"]),
                ).fetchone()
            ):
                raise Blocked("Contact suppressed, invalid or closed")
            if contact["state"] not in {"AWAITING_REPLY", "ENGAGED", "REVIEW", "MEETING_BOOKED"}:
                raise Blocked("Contact state does not allow booking")
            booked = db.execute(
                "select event_id from meetings where contact_id=? and status='BOOKED'", (contact_id,)
            ).fetchone()
            if booked and booked["event_id"] != event_id:
                raise Blocked("Contact already has a booked meeting; rescheduling requires operator review")
            existing = self.calendar.get_event(campaign.calendar_id, event_id)
            if existing:
                self._save(db, contact, start, end, event_id)
                return existing
            local, finish = (
                start.astimezone(ZoneInfo(campaign.timezone)),
                end.astimezone(ZoneInfo(campaign.timezone)),
            )
            if (
                start <= now()
                or local.weekday() >= 5
                or local.hour < campaign.working_start
                or (finish.hour + finish.minute / 60 > campaign.working_end)
                or local.date() != finish.date()
            ):
                raise Blocked("Slot outside future working hours")
            busy = self.calendar.freebusy(campaign.calendar_id, start, end)
            if any(start < b and end > a for a, b in busy):
                raise Blocked("Time is no longer available")
            # Other apps may race free/busy; Google has no atomic availability-and-insert API.
            body = {
                "id": event_id,
                "summary": f"AI workflow conversation with {contact['name'] or contact['domain']}",
                "start": {"dateTime": start.isoformat(), "timeZone": campaign.timezone},
                "end": {"dateTime": end.isoformat(), "timeZone": campaign.timezone},
                "attendees": [{"email": contact["email"]}],
                "description": "Discuss the workflow, desired result and whether a bounded assessment would help.",
            }
            result = self.calendar.insert(campaign.calendar_id, body)
            self._save(db, contact, start, end, event_id)
            return result

    def _save(self, db, contact, start, end, event_id):
        db.execute(
            """insert or ignore into meetings(id,contact_id,start,end,event_id,status,brief)
            values (?,?,?,?,?,'BOOKED',?)""",
            (
                event_id,
                contact["id"],
                start.isoformat(),
                end.isoformat(),
                event_id,
                "Review sourced research; ask about current workflow, volume, constraints and desired outcomes.",
            ),
        )
        state = db.execute("select state from contacts where id=?", (contact["id"],)).fetchone()[0]
        if state == "AWAITING_REPLY":
            self.app.transition(contact["id"], "ENGAGED", db)
        if state != "MEETING_BOOKED":
            self.app.transition(contact["id"], "MEETING_BOOKED", db)
        db.execute("update contacts set reply_hold=1 where id=?", (contact["id"],))
        db.execute("update followups set status='CANCELLED' where contact_id=?", (contact["id"],))
        db.execute(
            "update messages set status='CANCELLED' where contact_id=? and category='followup' and status in ('REVIEW','APPROVED')",
            (contact["id"],),
        )
        self.app.store.audit("meeting.booked", contact["id"], {"event_id": event_id}, db)
