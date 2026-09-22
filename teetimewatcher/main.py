#!/usr/bin/env python3
"""
Tee Time Watcher
================
Watches public tee-time availability pages for a list of golf courses and
sends a phone notification the moment a matching time slot appears, so you
can jump in and book it yourself, by hand, within seconds of it going live.

WHAT THIS DOES NOT DO (on purpose):
  - It does not log in to any course's account.
  - It does not enter payment info.
  - It does not submit a reservation for you.
It only reads the same publicly visible availability page you'd see in a
browser, and tells you the instant something changes. You still click
"Book" yourself.

Run `python main.py --once --debug` first to test one pass and inspect
the debug_dumps/ folder before leaving this running unattended.

Full setup instructions are in README.md.
"""

import json
import os
import re
import time
import random
import sys
import argparse
from datetime import date, timedelta, datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
WATCHES_PATH = HERE / "watches.json"
STATE_PATH = HERE / "state.json"
DEBUG_DIR = HERE / "debug_dumps"

# Matches times like "7:15 AM", "07:15am", "11:45 PM"
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9]):([0-5][0-9])\s*([AaPp][Mm])\b')


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def send_notification(topic, title, message, click_url=None):
    """Sends a push notification via ntfy.sh (free, no account needed)."""
    headers = {"Title": title.encode("utf-8"), "Content-Type": "text/plain; charset=utf-8"}
    if click_url:
        headers["Click"] = click_url
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        print(f"  [notified] {title}")
    except Exception as e:
        print(f"  [!] Failed to send notification: {e}")


def upcoming_target_dates(max_days_ahead, weekdays):
    """Dates within max_days_ahead whose weekday is in `weekdays` (Mon=0 .. Sun=6)."""
    today = date.today()
    return [
        today + timedelta(days=i)
        for i in range(0, max_days_ahead + 1)
        if (today + timedelta(days=i)).weekday() in weekdays
    ]


def in_time_window(hour, minute, start_hour, end_hour):
    total = hour * 60 + minute
    return start_hour * 60 <= total <= end_hour * 60


def parse_time_str(time_str):
    m = TIME_RE.search(time_str)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3).lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return hour, minute


def extract_time_strings(text):
    return [m.group(0) for m in TIME_RE.finditer(text)]


def build_url(course, target_date):
    """For courses whose booking page takes the date as a URL parameter."""
    if course.get("nav_style") == "url_param":
        param = course.get("date_param", "date")
        date_format = course.get("date_format", "%Y-%m-%d")
        date_str = target_date.strftime(date_format)
        # quote_plus matches how GolfNow's own links encode dates (spaces -> '+'),
        # and leaves '/' alone so CPS-golf-style m/d/Y dates still look normal.
        date_str = quote_plus(date_str, safe="/")
        base = course["url"]
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{param}={date_str}"
    return course["url"]


def fetch_page_text(playwright, course, target_date, debug=False):
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        )
        url = build_url(course, target_date)
        page.goto(url, timeout=30000)
        page.wait_for_timeout(3000)  # let the page's JS render the tee sheet

        # Some booking widgets are single-page apps with a "next day" arrow
        # instead of a URL you can jump to directly. For those, click forward
        # from today to the target date.
        if course.get("nav_style") == "click_next_day":
            days_forward = (target_date - date.today()).days
            selector = course.get("next_day_selector")
            for _ in range(days_forward):
                try:
                    page.click(selector, timeout=5000)
                    page.wait_for_timeout(1500)
                except Exception:
                    print(f"  [!] Could not click next-day control for {course['name']} "
                          f"(selector may need fixing) — see README troubleshooting.")
                    break

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        text = page.inner_text("body")

        if debug:
            DEBUG_DIR.mkdir(exist_ok=True)
            safe_name = course["name"].replace(" ", "_")
            (DEBUG_DIR / f"{safe_name}_{target_date}.txt").write_text(text, encoding="utf-8")

        return text, url
    finally:
        browser.close()


def check_course(playwright, course, watches_for_course, state, ntfy_topic, debug=False):
    name = course["name"]
    max_days = course.get("max_days_ahead", 7)
    weekdays = course.get("weekdays", [5, 6])  # Saturday, Sunday by default
    default_start = course.get("start_hour", 6)
    default_end = course.get("end_hour", 11)

    # Standing recurring dates (e.g. "every weekend morning") get the
    # course's default time window.
    date_windows = {d: (default_start, default_end) for d in upcoming_target_dates(max_days, weekdays)}

    # One-off custom watches (from watches.json) layer on top, and can
    # cover a date outside the recurring pattern entirely (a weekday, or
    # further out than the course's normal window) and/or override the
    # time window just for that date.
    for w in watches_for_course:
        try:
            d = date.fromisoformat(w["date"])
        except (KeyError, ValueError):
            print(f"  [!] Skipping malformed watch entry for {name}: {w}")
            continue
        if d < date.today():
            continue  # past date, ignore
        date_windows[d] = (w.get("start_hour", default_start), w.get("end_hour", default_end))

    course_state = state.setdefault(name, {})

    for target_date in sorted(date_windows):
        start_hour, end_hour = date_windows[target_date]
        date_key = target_date.isoformat()
        try:
            text, url = fetch_page_text(playwright, course, target_date, debug=debug)
        except Exception as e:
            print(f"  [!] {name} ({date_key}): could not load page: {e}")
            continue

        matching = set()
        for raw in extract_time_strings(text):
            parsed = parse_time_str(raw)
            if parsed and in_time_window(parsed[0], parsed[1], start_hour, end_hour):
                matching.add(f"{parsed[0]:02d}:{parsed[1]:02d}")

        seen_before = set(course_state.get(date_key, []))
        new_times = matching - seen_before

        if new_times:
            times_str = ", ".join(sorted(new_times))
            print(f"  [NEW TIME] {name} {date_key}: {times_str}")
            send_notification(
                ntfy_topic,
                title=f"Tee time open: {name}",
                message=f"{date_key} — {times_str}\n{url}",
                click_url=url,
            )
        else:
            print(f"  [ok] {name} {date_key}: {len(matching)} matching time(s), nothing new")

        course_state[date_key] = sorted(matching)

    state[name] = course_state


def main():
    parser = argparse.ArgumentParser(description="Tee Time Watcher")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    parser.add_argument("--debug", action="store_true",
                         help="Save each page's visible text to debug_dumps/ for troubleshooting")
    parser.add_argument("--interval", type=int, default=None, help="Override poll interval in seconds")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, None)
    if config is None:
        print(f"Missing {CONFIG_PATH}. See README.md.")
        sys.exit(1)

    ntfy_topic = os.environ.get("NTFY_TOPIC") or config["ntfy_topic"]
    interval = args.interval or config.get("poll_interval_seconds", 60)
    courses = [c for c in config["courses"] if c.get("enabled", True)]

    if ntfy_topic in ("", "CHANGE-ME-pick-a-private-topic-name"):
        print("Please set a real, private ntfy_topic in config.json first (see README.md).")
        sys.exit(1)

    state = load_json(STATE_PATH, {})
    watches = load_json(WATCHES_PATH, [])
    watches_by_course = {}
    for w in watches:
        watches_by_course.setdefault(w.get("course"), []).append(w)

    print(f"Tee Time Watcher starting.")
    print(f"Notifications -> https://ntfy.sh/{ntfy_topic}")
    print(f"Watching {len(courses)} course(s), polling every {interval}s.")
    if watches:
        print(f"Plus {len(watches)} custom one-off watch(es) from watches.json.")
    print()

    while True:
        cycle_start = time.time()
        print(f"--- Check cycle: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
        with sync_playwright() as p:
            for course in courses:
                print(f"Checking {course['name']}...")
                course_watches = watches_by_course.get(course["name"], [])
                check_course(p, course, course_watches, state, ntfy_topic, debug=args.debug)
                time.sleep(random.uniform(1, 3))  # be gentle between courses
        save_json(STATE_PATH, state)
        print()

        if args.once:
            break

        elapsed = time.time() - cycle_start
        time.sleep(max(5, interval - elapsed))


if __name__ == "__main__":
    main()
