# Triathlon event calendar feed

A calendar feed (ICS) of all events on <https://www.uds-triathlon.de/events/>, so you can see them in Outlook next to your own appointments.

**Feed URL:** `https://lrmyr.github.io/triathlon-event-calendar/events.ics`

The feed is read-only and rebuilt every 4 hours. It is unofficial: the website stays the source of truth.

## Subscribe in Outlook

Subscribe to the URL; do not download and import the file, or it will never update.

**Outlook on the web and new Outlook for Windows**

1. Open the calendar.
2. Select **Add calendar** → **Subscribe from web**.
3. Paste the feed URL, give the calendar a name (e.g. "Triathlon Events"), and select **Import**.

**Classic Outlook for Windows**

1. Open the calendar.
2. On the **Home** tab select **Add Calendar** (or **Open Calendar**) → **From Internet…**
3. Paste the feed URL, select **OK**, and confirm with **Yes**.

The calendar then appears under "Other calendars" and syncs to your other devices through your mailbox.

## What you get

- One entry per session. An event with three dates shows up three times.
- Title, location, the short description, a link to the event page, and the event type (Vor Ort, Webinar, FabLab, Hybrid) as category.
- Times are in German local time (Europe/Berlin).
- Sessions disappear from the feed 30 days after they ended.

## Known limitations

- **Courses and other multi-day events appear only on their first day.** When the website gives a date range without times (e.g. "17.09.26 – 05.10.26"), the feed has one all-day entry on the start date. The full range is in the description; the individual sessions are only on the event page.
- **Changes take time to arrive.** The feed is rebuilt every 4 hours, and Outlook fetches subscribed calendars on its own schedule, which can be several hours up to a day. You cannot force a refresh in Outlook on the web.
- **Only the short description is included**, not the full text of the event page. Registration happens on the website.
- **A session whose start time changes** gets a new ID. Outlook then replaces the old entry with the new one at its next refresh.
- **GitHub pauses scheduled workflows** in repositories without any activity for 60 days. GitHub sends an email first; re-enable the workflow under *Actions* if that happens.

## How it works

The website has no calendar feed and no API for event dates. But every event page has an "Event im Kalender speichern" button, and the data it sends sits in the page as hidden form fields. `build_feed.py` reads those:

1. Collect the event URLs from the sitemap (`/event-sitemap.xml`) and the WordPress REST API (`/wp-json/wp/v2/events`). Both are needed: each one misses events the other has.
2. Fetch each event page (one request per second, with an identifying User-Agent) and read the form fields, the post ID, and the event type.
3. Correct two errors in the embedded dates:
   - Times are marked as UTC (`Z`) but are really German local time.
   - When an event has no end date, the site fills in the date the page was rendered. The feed uses the session's start date instead.
4. Write `events.ics`, one `VEVENT` per session, with the ID `<post-id>-<session-start>@uds-triathlon.de`.

A GitHub Actions workflow (`.github/workflows/feed.yml`) runs this every 4 hours, checks the result with `validate_feed.py`, and publishes it with GitHub Pages. If the website is unreachable or more than 20 % of the pages fail, the run fails and the last good feed stays online.

Each run ends with a summary in the workflow log: events found, sessions written, sessions skipped, date-range events, and pages that could not be read, with the reason.

## Run it locally

Building needs Python 3.10+ and nothing else. Tests and validation need the `icalendar` package.

```sh
python build_feed.py --output public/events.ics

pip install -r requirements-dev.txt
python -m unittest discover -s tests
python validate_feed.py public/events.ics
```

To run the workflow by hand: *Actions* → *Build and publish feed* → *Run workflow*.
