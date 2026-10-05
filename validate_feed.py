#!/usr/bin/env python3
"""Re-parse events.ics with the icalendar library and check what Outlook needs."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import icalendar


def validate(data: bytes) -> list[str]:
    """Returns a list of problems; empty means the feed is fine."""
    problems = []

    if b"\r\n" not in data or b"\n" in data.replace(b"\r\n", b""):
        problems.append("lines must end in CRLF")
    for number, line in enumerate(data.split(b"\r\n"), 1):
        if len(line) > 75:
            problems.append(f"line {number} is longer than 75 octets")

    try:
        calendar = icalendar.Calendar.from_ical(data)
    except ValueError as error:
        return problems + [f"icalendar cannot parse the feed: {error}"]

    for name in ("VERSION", "PRODID", "X-WR-CALNAME"):
        if not calendar.get(name):
            problems.append(f"calendar is missing {name}")
    timezones = [str(tz.get("TZID")) for tz in calendar.walk("VTIMEZONE")]
    if timezones != ["Europe/Berlin"]:
        problems.append(f"expected one VTIMEZONE Europe/Berlin, found {timezones}")

    uids = set()
    for event in calendar.walk("VEVENT"):
        uid = str(event.get("UID", ""))
        where = f"event {uid or '?'}"
        if not uid:
            problems.append("event without UID")
        elif uid in uids:
            problems.append(f"{where}: duplicate UID")
        uids.add(uid)

        for name in ("DTSTAMP", "DTSTART", "DTEND", "SUMMARY"):
            if not event.get(name):
                problems.append(f"{where}: missing {name}")
        if not event.get("DTSTART") or not event.get("DTEND"):
            continue

        start, end = event["DTSTART"], event["DTEND"]
        if isinstance(start.dt, datetime):
            # floating or UTC times would shift in Outlook
            for name, prop in (("DTSTART", start), ("DTEND", end)):
                if prop.params.get("TZID") != "Europe/Berlin":
                    problems.append(f"{where}: {name} is not in TZID=Europe/Berlin")
            if not isinstance(end.dt, datetime) or end.dt <= start.dt:
                problems.append(f"{where}: DTEND is not after DTSTART")
        elif isinstance(start.dt, date):
            if start.params.get("VALUE") != "DATE" or end.params.get("VALUE") != "DATE":
                problems.append(f"{where}: all-day event without VALUE=DATE")
            # Outlook shows nothing for an all-day event whose end isn't exclusive
            elif end.dt < start.dt + timedelta(days=1):
                problems.append(f"{where}: all-day DTEND must be the day after the last day")

    return problems


def main(argv=None) -> int:
    path = Path((argv or sys.argv[1:] or ["events.ics"])[0])
    data = path.read_bytes()
    problems = validate(data)
    for problem in problems:
        print(f"PROBLEM {problem}")
    if problems:
        return 1
    events = icalendar.Calendar.from_ical(data).walk("VEVENT")
    print(f"{path}: OK, {len(events)} events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
