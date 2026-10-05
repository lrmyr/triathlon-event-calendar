#!/usr/bin/env python3
"""Build an ICS feed of all events on www.uds-triathlon.de.

The site has no calendar feed, but every event page embeds its data as hidden
form fields for the "Event im Kalender speichern" button. This script collects
the event URLs (sitemap + REST API), reads those fields and writes one
events.ics. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://www.uds-triathlon.de"
SITEMAP_URL = f"{BASE}/event-sitemap.xml"
API_URL = f"{BASE}/wp-json/wp/v2/events"
EVENT_URL_RE = re.compile(rf"^{re.escape(BASE)}/events/[^/]+/$")
USER_AGENT = (
    "UdS-Triathlon-ICS-Feed/1.0 "
    "(internal calendar feed; +https://github.com/lrmyr/triathlon-event-calendar)"
)
UID_DOMAIN = "uds-triathlon.de"
BERLIN = ZoneInfo("Europe/Berlin")
CALENDAR_NAME = "Triathlon Events"
REFRESH = "PT4H"

MAX_AGE_DAYS = 30
# The site puts the page-render date into date_end when an event has no end
# date. Pages are cached, so that date can lag behind today by a bit.
PLACEHOLDER_WINDOW_DAYS = 3
# Refuse to write a feed when more than this share of pages failed, so a site
# outage doesn't empty everyone's calendar.
MAX_FAILURE_RATIO = 0.2

VTIMEZONE = """\
BEGIN:VTIMEZONE
TZID:Europe/Berlin
X-LIC-LOCATION:Europe/Berlin
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE""".splitlines()


# ---------------------------------------------------------------- fetching


class Fetcher:
    """GET with a custom User-Agent and a pause between requests."""

    def __init__(self, delay: float = 1.0, retries: int = 2):
        self.delay = delay
        self.retries = retries
        self._last = 0.0

    def get(self, url: str) -> tuple[bytes, dict]:
        for attempt in range(self.retries + 1):
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read(), dict(response.headers)
            except urllib.error.HTTPError as error:
                # 4xx won't get better by retrying
                if error.code < 500 or attempt == self.retries:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == self.retries:
                    raise
            finally:
                self._last = time.monotonic()
            time.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")


def discover_event_urls(fetcher: Fetcher, log=print) -> list[str]:
    """Union of sitemap and REST API: each one misses events the other has."""
    urls: set[str] = set()
    sources_ok = 0

    try:
        body, _ = fetcher.get(SITEMAP_URL)
        found = set(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body.decode("utf-8")))
        found = {u for u in found if EVENT_URL_RE.match(u)}
        log(f"sitemap: {len(found)} event URLs")
        urls |= found
        sources_ok += 1
    except Exception as error:  # noqa: BLE001 - the other source may still work
        log(f"WARNING sitemap unavailable: {error}")

    try:
        found, page, pages = set(), 1, 1
        while page <= pages:
            body, headers = fetcher.get(f"{API_URL}?per_page=100&page={page}&_fields=id,link")
            headers = {k.lower(): v for k, v in headers.items()}
            pages = int(headers.get("x-wp-totalpages", 1))
            found |= {item["link"] for item in json.loads(body)}
            page += 1
        found = {u for u in found if EVENT_URL_RE.match(u)}
        log(f"REST API: {len(found)} event URLs")
        urls |= found
        sources_ok += 1
    except Exception as error:  # noqa: BLE001
        log(f"WARNING REST API unavailable: {error}")

    if not sources_ok:
        raise RuntimeError("neither sitemap nor REST API could be read")
    return sorted(urls)


# ----------------------------------------------------------------- parsing


@dataclass
class Event:
    post_id: str
    summary: str
    url: str
    location: str = ""
    description: str = ""
    categories: list[str] = field(default_factory=list)
    # raw (date_start, date_end) pairs exactly as embedded in the page
    raw_sessions: list[tuple[str, str]] = field(default_factory=list)


class _EventPageParser(HTMLParser):
    """Pulls the hidden download-ics.php form fields, post ID and event type."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.post_id = None
        self.fields: dict[str, list[str]] = {}
        self.types: list[str] = []
        self._in_form = False
        self._seen_h1 = False
        self._header_done = False
        self._in_type = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "body":
            match = re.search(r"\bpostid-(\d+)\b", attrs.get("class") or "")
            if match:
                self.post_id = match.group(1)
        elif tag == "form":
            self._in_form = (attrs.get("action") or "").endswith("download-ics.php")
        elif tag == "input" and self._in_form and attrs.get("name"):
            self.fields.setdefault(attrs["name"], []).append(attrs.get("value") or "")
        elif tag == "h1":
            self._seen_h1 = True
        elif self._seen_h1 and not self._header_done:
            # the event's own type badges follow its <h1> directly; later
            # "type" spans belong to the related-events teasers
            if tag == "span" and "type" in (attrs.get("class") or "").split():
                self._in_type = True
            elif tag != "span":
                self._header_done = True

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
        elif tag == "span":
            self._in_type = False

    def handle_data(self, data):
        if self._in_type and data.strip():
            self.types.append(data.strip())


def parse_event_page(html: str) -> Event:
    """Raises ValueError with a reason when the page has no usable event data."""
    parser = _EventPageParser()
    parser.feed(html)
    fields = parser.fields

    if not fields:
        raise ValueError("no download-ics form found")
    if not parser.post_id:
        raise ValueError("no post ID found")
    starts = fields.get("date_start[]", [])
    ends = fields.get("date_end[]", [])
    if not starts:
        raise ValueError("no dates")
    if len(starts) != len(ends):
        raise ValueError(f"{len(starts)} start dates but {len(ends)} end dates")

    def one(name: str) -> str:
        return " ".join((fields.get(name) or [""])[0].split())

    if not one("summary"):
        raise ValueError("no title")
    return Event(
        post_id=parser.post_id,
        summary=one("summary"),
        url=one("url"),
        location=one("location").strip(" ,"),
        description=one("description"),
        categories=parser.types,
        raw_sessions=list(zip(starts, ends)),
    )


# ---------------------------------------------------------------- sessions


@dataclass
class Session:
    event: Event
    start: datetime | date
    end: datetime | date  # all-day: last day, inclusive
    all_day: bool = False
    span_end: date | None = None  # set for span events: real last day
    note: str = ""

    @property
    def uid(self) -> str:
        stamp = self.start.strftime("%Y%m%d" if self.all_day else "%Y%m%dT%H%M%S")
        return f"{self.event.post_id}-{stamp}@{UID_DOMAIN}"

    @property
    def ended(self) -> datetime:
        """Moment the session (or the span it stands for) is over."""
        if self.all_day:
            last = self.span_end or self.end
            return datetime.combine(last + timedelta(days=1), datetime.min.time(), BERLIN)
        return self.end


def _parse_stamp(value: str) -> datetime:
    # The trailing "Z" is wrong: the site emits local Berlin wall-clock time.
    return datetime.strptime(value.strip().rstrip("Z"), "%Y%m%dT%H%M%S")


def build_session(event: Event, raw_start: str, raw_end: str, today: date) -> Session:
    """Turn one embedded start/end pair into a corrected session.

    Raises ValueError when the pair can't be parsed.
    """
    start, end = _parse_stamp(raw_start), _parse_stamp(raw_end)
    midnight = datetime.min.time()

    if start.time() == midnight:
        # No time of day set: an all-day entry. A real end date is also at
        # midnight; anything else is the page-render timestamp placeholder.
        if end.time() == midnight and end.date() > start.date():
            return Session(
                event, start.date(), start.date(), all_day=True, span_end=end.date(),
                note=f"span {start:%d.%m.%Y} - {end:%d.%m.%Y}, all-day entry on start date",
            )
        return Session(event, start.date(), start.date(), all_day=True)

    note = ""
    if end.date() != start.date():
        placeholder_from = today - timedelta(days=PLACEHOLDER_WINDOW_DAYS)
        looks_like_render_date = placeholder_from <= end.date() <= today + timedelta(days=1)
        if end < start or looks_like_render_date:
            end = datetime.combine(start.date(), end.time())
        else:
            note = f"timed multi-day session until {end:%d.%m.%Y %H:%M}"
    if end <= start:
        end = start + timedelta(hours=1)
        note = "end time not after start time, assumed 1 hour"
    return Session(event, start.replace(tzinfo=BERLIN), end.replace(tzinfo=BERLIN), note=note)


# --------------------------------------------------------------------- ICS


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold to 75 octets per line without splitting a UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, limit = [], 75
    while len(raw) > limit:
        cut = limit
        while (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
        limit = 74  # continuation lines start with a space
    parts.append(raw.decode("utf-8"))
    return "\r\n ".join(parts)


def _description(session: Session) -> str:
    event = session.event
    parts = []
    if session.span_end:
        parts.append(
            f"Zeitraum: {session.start:%d.%m.%Y} – {session.span_end:%d.%m.%Y}. "
            "Die einzelnen Termine stehen auf der Eventseite."
        )
    if event.description:
        parts.append(event.description)
    if event.url:
        parts.append(f"Mehr Infos: {event.url}")
    return "\n\n".join(parts)


def session_to_vevent(session: Session, now: datetime) -> list[str]:
    event = session.event
    lines = [
        "BEGIN:VEVENT",
        f"UID:{session.uid}",
        f"DTSTAMP:{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
    ]
    if session.all_day:
        lines += [
            f"DTSTART;VALUE=DATE:{session.start:%Y%m%d}",
            f"DTEND;VALUE=DATE:{session.end + timedelta(days=1):%Y%m%d}",
            "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE",
            "TRANSP:TRANSPARENT",
        ]
    else:
        lines += [
            f"DTSTART;TZID=Europe/Berlin:{session.start:%Y%m%dT%H%M%S}",
            f"DTEND;TZID=Europe/Berlin:{session.end:%Y%m%dT%H%M%S}",
        ]
    lines.append(f"SUMMARY:{_escape(event.summary)}")
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    lines.append(f"DESCRIPTION:{_escape(_description(session))}")
    if event.url:
        lines.append(f"URL:{event.url}")
    if event.categories:
        lines.append("CATEGORIES:" + ",".join(_escape(c) for c in event.categories))
    lines.append("END:VEVENT")
    return lines


def render_calendar(sessions: list[Session], now: datetime) -> bytes:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//UdS Triathlon//Event Feed//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(CALENDAR_NAME)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH}",
        f"X-PUBLISHED-TTL:{REFRESH}",
        *VTIMEZONE,
    ]
    for session in sorted(sessions, key=lambda s: (s.ended, s.uid)):
        lines += session_to_vevent(session, now)
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode("utf-8")


# -------------------------------------------------------------------- main


def collect_sessions(pages, now: datetime, log=print):
    """pages: iterable of (url, html or Exception). Returns (sessions, stats)."""
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    sessions: list[Session] = []
    seen_posts: set[str] = set()
    seen_uids: set[str] = set()
    stats = {
        "urls": 0, "events": 0, "events_in_feed": 0, "duplicates": 0,
        "sessions_old": 0, "failed": [], "unparseable": [], "flagged": [],
    }

    for url, html in pages:
        stats["urls"] += 1
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if isinstance(html, Exception):
            stats["failed"].append(f"{slug}: {html}")
            continue
        try:
            event = parse_event_page(html)
        except ValueError as error:
            stats["unparseable"].append(f"{slug}: {error}")
            continue
        # renamed events stay in the sitemap under their old URL
        if event.post_id in seen_posts:
            stats["duplicates"] += 1
            continue
        seen_posts.add(event.post_id)
        stats["events"] += 1
        event.url = event.url or url

        written = 0
        for raw_start, raw_end in event.raw_sessions:
            try:
                session = build_session(event, raw_start, raw_end, now.date())
            except ValueError:
                stats["unparseable"].append(f"{slug}: bad date {raw_start!r} / {raw_end!r}")
                continue
            if session.ended < cutoff:
                stats["sessions_old"] += 1
                continue
            if session.uid in seen_uids:
                continue
            seen_uids.add(session.uid)
            sessions.append(session)
            written += 1
            if session.note:
                stats["flagged"].append(f"{slug}: {session.note}")
        stats["events_in_feed"] += bool(written)

    return sessions, stats


def print_summary(stats: dict, sessions: list[Session], log=print) -> None:
    log("")
    log("Run summary")
    log(f"  event URLs checked:        {stats['urls']}")
    log(f"  events found:              {stats['events']}")
    log(f"  duplicate URLs (renamed):  {stats['duplicates']}")
    log(f"  events in feed:            {stats['events_in_feed']}")
    log(f"  sessions written:          {len(sessions)}")
    log(f"  sessions skipped (ended > {MAX_AGE_DAYS} days ago): {stats['sessions_old']}")
    for title, key in (
        ("flagged (check manually)", "flagged"),
        ("unparseable", "unparseable"),
        ("fetch failed", "failed"),
    ):
        log(f"  {title}: {len(stats[key])}")
        for entry in stats[key]:
            log(f"    - {entry}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", default="events.ics", type=Path)
    parser.add_argument("--delay", default=1.0, type=float,
                        help="seconds between requests (default: 1)")
    args = parser.parse_args(argv)

    now = datetime.now(BERLIN)
    fetcher = Fetcher(delay=args.delay)
    try:
        urls = discover_event_urls(fetcher)
    except RuntimeError as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1

    def pages():
        for index, url in enumerate(urls, 1):
            try:
                body, _ = fetcher.get(url)
                yield url, body.decode("utf-8", errors="replace")
            except Exception as error:  # noqa: BLE001 - reported in the summary
                yield url, error
            if index % 20 == 0:
                print(f"  fetched {index}/{len(urls)} pages")

    sessions, stats = collect_sessions(pages(), now)
    print_summary(stats, sessions)

    broken = len(stats["failed"]) + len(stats["unparseable"])
    if not stats["events"] or broken > MAX_FAILURE_RATIO * stats["urls"]:
        print("ERROR too many pages failed; not writing a feed", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(render_calendar(sessions, now))
    print(f"\nwrote {args.output} ({len(sessions)} sessions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
