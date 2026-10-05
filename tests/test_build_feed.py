"""Offline tests: synthetic event pages in the shapes the site really emits."""

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import icalendar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_feed  # noqa: E402
from validate_feed import validate  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=build_feed.BERLIN)
TODAY = NOW.date()


def page(post_id, summary, sessions, location="Innovation Center, Campus A2 1, 66123 Saarbrücken",
         description="Kurzbeschreibung", slug="test-event", event_type="Vor Ort"):
    dates = "".join(
        f'<input type="hidden" name="date_start[]" value="{start}">'
        f'<input type="hidden" name="date_end[]" value="{end}">'
        for start, end in sessions
    )
    return f"""<!DOCTYPE html><html lang="de-DE"><head><title>x</title></head>
<body class="single single-event postid-{post_id} wp-theme-fbo-triathlon">
<h1>{summary}</h1><span class="type">{event_type}</span><span class="place">Ort: X</span>
<p>Teaser</p>
<form method="post" action="https://www.uds-triathlon.de/wp-content/themes/fbo-triathlon/includes/download-ics.php">
{dates}
<input type="hidden" name="location" value="{location}">
<input type="hidden" name="description" value="{description}">
<input type="hidden" name="summary" value="{summary}">
<input type="hidden" name="url" value="https://www.uds-triathlon.de/events/{slug}/">
</form>
<h2>Weitere passende Events</h2><span class="type">Webinar</span>
</body></html>"""


def build(pages):
    """Run pages through the pipeline and return (parsed calendar, stats, raw bytes)."""
    sessions, stats = build_feed.collect_sessions(pages, NOW, log=lambda *_: None)
    data = build_feed.render_calendar(sessions, NOW)
    return icalendar.Calendar.from_ical(data), stats, data


def events_of(calendar):
    return {str(e["UID"]): e for e in calendar.walk("VEVENT")}


class ParseTests(unittest.TestCase):
    def test_reads_form_fields_post_id_and_own_type(self):
        event = build_feed.parse_event_page(
            page(2187, "Community-Treffen &amp; Weißwurstfrühstück",
                 [("20261015T090000Z", "20261005T113000Z")])
        )
        self.assertEqual(event.post_id, "2187")
        self.assertEqual(event.summary, "Community-Treffen & Weißwurstfrühstück")
        self.assertEqual(event.raw_sessions, [("20261015T090000Z", "20261005T113000Z")])
        # not the "Webinar" badge of the related-events teaser
        self.assertEqual(event.categories, ["Vor Ort"])

    def test_location_without_address_is_tidied(self):
        event = build_feed.parse_event_page(
            page(1, "A", [("20261015T090000Z", "20261015T100000Z")], location="online, ")
        )
        self.assertEqual(event.location, "online")

    def test_page_without_form_is_reported(self):
        with self.assertRaisesRegex(ValueError, "no download-ics form"):
            build_feed.parse_event_page("<html><body class='postid-1'></body></html>")


class SessionTests(unittest.TestCase):
    def session(self, start, end):
        event = build_feed.Event(post_id="1", summary="A", url="u")
        return build_feed.build_session(event, start, end, TODAY)

    def test_z_suffix_is_read_as_berlin_time(self):
        session = self.session("20261015T090000Z", "20261015T113000Z")
        self.assertEqual(session.start, datetime(2026, 10, 15, 9, 0, tzinfo=build_feed.BERLIN))
        self.assertEqual(session.start.utcoffset(), timedelta(hours=2))

    def test_render_date_placeholder_becomes_same_day(self):
        # upcoming session, end carries today's date
        session = self.session("20261103T100000Z", "20261005T120000Z")
        self.assertEqual(session.end, datetime(2026, 11, 3, 12, 0, tzinfo=build_feed.BERLIN))
        self.assertEqual(session.note, "")

    def test_stale_cached_placeholder_on_upcoming_session(self):
        # page cached a week ago: end date is before the start, so still a placeholder
        session = self.session("20261103T100000Z", "20260928T120000Z")
        self.assertEqual(session.end.date(), date(2026, 11, 3))

    def test_placeholder_on_recent_past_session(self):
        session = self.session("20260922T170000Z", "20261005T200000Z")
        self.assertEqual(session.end, datetime(2026, 9, 22, 20, 0, tzinfo=build_feed.BERLIN))

    def test_real_timed_multi_day_is_kept_and_flagged(self):
        session = self.session("20260914T170000Z", "20260918T203000Z")
        self.assertEqual(session.end, datetime(2026, 9, 18, 20, 30, tzinfo=build_feed.BERLIN))
        self.assertIn("multi-day", session.note)

    def test_span_is_all_day_on_start_date(self):
        session = self.session("20261001T000000Z", "20270331T000000Z")
        self.assertTrue(session.all_day)
        self.assertEqual((session.start, session.end), (date(2026, 10, 1), date(2026, 10, 1)))
        self.assertEqual(session.span_end, date(2027, 3, 31))
        self.assertIn("span", session.note)

    def test_short_span_is_multi_day_all_day(self):
        session = self.session("20261113T000000Z", "20261115T000000Z")
        self.assertTrue(session.all_day)
        self.assertEqual((session.start, session.end), (date(2026, 11, 13), date(2026, 11, 15)))
        self.assertIsNone(session.span_end)
        self.assertEqual(session.note, "")

    def test_span_length_boundary(self):
        week = self.session("20261102T000000Z", "20261108T000000Z")  # 7 days
        self.assertEqual(week.end, date(2026, 11, 8))
        longer = self.session("20261102T000000Z", "20261109T000000Z")  # 8 days
        self.assertEqual(longer.end, date(2026, 11, 2))
        self.assertEqual(longer.span_end, date(2026, 11, 9))

    def test_date_only_without_end_is_single_all_day(self):
        session = self.session("20261020T000000Z", "20261005T055856Z")
        self.assertTrue(session.all_day)
        self.assertIsNone(session.span_end)
        self.assertEqual(session.end, date(2026, 10, 20))


class FeedTests(unittest.TestCase):
    def test_multi_session_event(self):
        calendar, stats, data = build([
            ("u", page(2187, "IP Office Hour", [
                ("20261006T100000Z", "20261005T120000Z"),
                ("20261103T100000Z", "20261005T120000Z"),
                ("20261201T100000Z", "20261005T120000Z"),
            ], slug="ip-office-hour")),
        ])
        self.assertEqual(validate(data), [])
        events = events_of(calendar)
        self.assertEqual(sorted(events), [
            "2187-20261006T100000@uds-triathlon.de",
            "2187-20261103T100000@uds-triathlon.de",
            "2187-20261201T100000@uds-triathlon.de",
        ])
        october = events["2187-20261006T100000@uds-triathlon.de"]
        self.assertEqual(october["DTSTART"].params["TZID"], "Europe/Berlin")
        self.assertEqual(october["DTSTART"].dt.replace(tzinfo=None), datetime(2026, 10, 6, 10, 0))
        self.assertEqual(october["DTEND"].dt.replace(tzinfo=None), datetime(2026, 10, 6, 12, 0))
        # summer time in October, winter time in December
        self.assertEqual(october["DTSTART"].dt.utcoffset(), timedelta(hours=2))
        december = events["2187-20261201T100000@uds-triathlon.de"]
        self.assertEqual(december["DTSTART"].dt.utcoffset(), timedelta(hours=1))

        self.assertEqual(str(october["SUMMARY"]), "IP Office Hour")
        self.assertEqual(str(october["LOCATION"]),
                         "Innovation Center, Campus A2 1, 66123 Saarbrücken")
        self.assertEqual(str(october["URL"]), "https://www.uds-triathlon.de/events/ip-office-hour/")
        self.assertIn("https://www.uds-triathlon.de/events/ip-office-hour/",
                      str(october["DESCRIPTION"]))
        self.assertEqual(october["CATEGORIES"].to_ical(), b"Vor Ort")
        self.assertEqual(stats["events"], 1)

    def test_span_event(self):
        calendar, stats, data = build([
            ("u", page(8722, "Kurs", [("20261001T000000Z", "20270331T000000Z")])),
        ])
        self.assertEqual(validate(data), [])
        (event,) = calendar.walk("VEVENT")
        self.assertEqual(str(event["UID"]), "8722-20261001@uds-triathlon.de")
        self.assertEqual(event["DTSTART"].dt, date(2026, 10, 1))
        self.assertEqual(event["DTEND"].dt, date(2026, 10, 2))
        self.assertIn("01.10.2026 – 31.03.2027", str(event["DESCRIPTION"]))
        self.assertEqual(len(stats["flagged"]), 1)

    def test_short_span_event(self):
        calendar, stats, data = build([
            ("u", page(3924, "Startup Weekend", [("20261113T000000Z", "20261115T000000Z")])),
        ])
        self.assertEqual(validate(data), [])
        (event,) = calendar.walk("VEVENT")
        self.assertEqual(event["DTSTART"].dt, date(2026, 11, 13))
        self.assertEqual(event["DTEND"].dt, date(2026, 11, 16))  # exclusive
        self.assertNotIn("Zeitraum", str(event["DESCRIPTION"]))
        self.assertEqual(stats["flagged"], [])

    def test_old_sessions_are_skipped(self):
        calendar, stats, _ = build([
            ("u", page(1, "Serie", [
                ("20260801T100000Z", "20261005T120000Z"),  # ended 2 months ago
                ("20260920T100000Z", "20261005T120000Z"),  # 15 days ago: kept
                ("20261020T100000Z", "20261005T120000Z"),
            ])),
            # span that started long ago but is still running: kept
            ("u2", page(2, "Laufender Kurs", [("20260401T000000Z", "20261231T000000Z")])),
            ("u3", page(3, "Alter Kurs", [("20260101T000000Z", "20260301T000000Z")])),
        ])
        self.assertEqual(sorted(events_of(calendar)), [
            "1-20260920T100000@uds-triathlon.de",
            "1-20261020T100000@uds-triathlon.de",
            "2-20260401@uds-triathlon.de",
        ])
        self.assertEqual(stats["sessions_old"], 2)
        self.assertEqual(stats["events_in_feed"], 2)

    def test_renamed_event_is_not_duplicated(self):
        html = page(4408, "CAD Basics", [("20261022T150000Z", "20261005T170000Z")])
        calendar, stats, _ = build([("old-url", html), ("new-url", html)])
        self.assertEqual(len(calendar.walk("VEVENT")), 1)
        self.assertEqual(stats["duplicates"], 1)

    def test_failures_are_counted_not_fatal(self):
        calendar, stats, _ = build([
            ("https://x/events/broken/", "<html><body class='postid-9'></body></html>"),
            ("https://x/events/down/", TimeoutError("timed out")),
            ("u", page(1, "A", [("20261020T100000Z", "20261005T120000Z")])),
        ])
        self.assertEqual(len(calendar.walk("VEVENT")), 1)
        self.assertEqual(len(stats["unparseable"]), 1)
        self.assertEqual(len(stats["failed"]), 1)
        self.assertIn("broken", stats["unparseable"][0])

    def test_special_characters_and_long_lines_survive(self):
        text = "Gründer:innen; Start-ups, Partner – " + "sehr lange Beschreibung äöü " * 20
        calendar, _, data = build([
            ("u", page(1, "Q&amp;A: Recht, Steuern; mehr",
                       [("20261020T100000Z", "20261005T120000Z")], description=text)),
        ])
        self.assertEqual(validate(data), [])
        (event,) = calendar.walk("VEVENT")
        self.assertEqual(str(event["SUMMARY"]), "Q&A: Recht, Steuern; mehr")
        self.assertTrue(str(event["DESCRIPTION"]).startswith(text.strip()))

    def test_empty_feed_is_still_valid(self):
        _, _, data = build([])
        self.assertEqual(validate(data), [])


class ValidatorTests(unittest.TestCase):
    def test_catches_utc_times_and_duplicate_uids(self):
        _, _, data = build([("u", page(1, "A", [("20261020T100000Z", "20261005T120000Z")]))])
        bad = data.replace(b"DTSTART;TZID=Europe/Berlin:20261020T100000",
                           b"DTSTART:20261020T100000Z")
        self.assertTrue(any("TZID" in p for p in validate(bad)))
        vevent = data[data.index(b"BEGIN:VEVENT"):data.index(b"END:VEVENT") + 12]
        doubled = data.replace(b"END:VCALENDAR", vevent + b"END:VCALENDAR")
        self.assertTrue(any("duplicate UID" in p for p in validate(doubled)))


if __name__ == "__main__":
    unittest.main()
