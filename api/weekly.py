"""
Vercel serverless function: weekly report cron.
File path in the repo must be: api/weekly.py

Triggered automatically once a week by Vercel Cron Jobs (see vercel.json).
Sends each "active chat" a report of the last 7 days: how many films each
tracked user logged, and the top 5 films by rating across the whole group.

This mirrors the on-demand /top command in api/webhook.py.
"""

import os
import sys
import json
import html
import time
import calendar
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta

import feedparser
import requests

RSS_URL = "https://letterboxd.com/{username}/rss/"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
WEEKLY_LOOKBACK_HOURS = 24 * 7

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
FALLBACK_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

REDIS_URL = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
REDIS_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]
USERS_KEY_PREFIX = "letterboxd:users:"
ACTIVE_CHATS_KEY = "letterboxd:active_chats"

ORIGINAL_CHAT_ID = "-5008110929"
DEFAULT_USERS = {
    "andrea beni": "andreonbenon",
    "Lorenzo Sartor": "lorenzosartor",
    "Davide Colli": "david_hills",
    "Francesco Zanatta": "zanfrancesco",
    "Simone Miglio": "ildivinatore01",
    "grampasso": "grampasso",
    "Antonio Orrico": "antonioorrico",
    "Emanuele Polverino": "EmaPolve",
}


def redis_get(key):
    resp = requests.get(
        f"{REDIS_URL}/get/{key}",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("result")


def redis_set(key, value):
    encoded = quote(value, safe="")
    resp = requests.post(
        f"{REDIS_URL}/set/{key}/{encoded}",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
        timeout=10,
    )
    resp.raise_for_status()


def get_users(chat_id):
    key = f"{USERS_KEY_PREFIX}{chat_id}"
    raw = redis_get(key)
    if raw is not None:
        return json.loads(raw)

    if str(chat_id) == ORIGINAL_CHAT_ID:
        seed = dict(DEFAULT_USERS)
    else:
        seed = {}
    redis_set(key, json.dumps(seed))
    return seed


def get_active_chats():
    raw = redis_get(ACTIVE_CHATS_KEY)
    chats = []
    if raw:
        try:
            chats = json.loads(raw)
        except json.JSONDecodeError:
            chats = []
    if not chats and FALLBACK_CHAT_ID:
        chats = [FALLBACK_CHAT_ID]
    return chats


def stars_from_rating(rating):
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        return ""
    full = int(rating)
    half = rating - full >= 0.5
    return "★" * full + ("½" if half else "")


def _entry_date_utc(entry):
    """Best-effort UTC datetime for an entry.

    Prefers the explicit letterboxd:watchedDate (the day the film was
    actually watched, plain YYYY-MM-DD, no timezone ambiguity) over the
    RSS pubDate. pubDate reflects when the diary log was *published*,
    which can be much later than the watch date (e.g. someone rating a
    backlog of old films in one sitting), and would otherwise make old
    watches look like they happened "this week".
    """
    watched = entry.get("letterboxd_watcheddate")
    if watched:
        try:
            return datetime.strptime(watched, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return None
    # feedparser normalizes published_parsed/updated_parsed to UTC already;
    # calendar.timegm (unlike time.mktime) treats the struct as UTC instead
    # of local time, so it doesn't shift by the server's timezone.
    return datetime.fromtimestamp(calendar.timegm(published), tz=timezone.utc)


def fetch_new_entries(username, since):
    url = RSS_URL.format(username=username)
    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        print(f"[warn] could not read feed for {username}: {feed.bozo_exception}", file=sys.stderr)
        return []

    new_entries = []
    seen = set()
    for entry in feed.entries:
        entry_dt = _entry_date_utc(entry)
        if not entry_dt or entry_dt < since:
            continue

        # Letterboxd's RSS feed can list the same diary log more than once
        # (e.g. after the entry is edited). Skip repeats of the same film
        # logged on the same watched date so it isn't counted twice.
        dedup_key = (
            entry.get("letterboxd_filmtitle"),
            entry.get("letterboxd_filmyear"),
            entry.get("letterboxd_watcheddate"),
        )
        if dedup_key[0] and dedup_key in seen:
            continue
        seen.add(dedup_key)

        new_entries.append(entry)

    new_entries.reverse()
    return new_entries


def collect_entries(chat_id, since):
    users = get_users(chat_id)
    collected = []
    for display_name, username in users.items():
        for entry in fetch_new_entries(username, since):
            collected.append((display_name, entry))
    return collected


def build_weekly_report_text(collected):
    if not collected:
        return "\U0001f4ca <b>Report settimanale</b>\n\nNessuna attività Letterboxd negli ultimi 7 giorni."

    counts = {}
    for display_name, _entry in collected:
        counts[display_name] = counts.get(display_name, 0) + 1
    ranked_counts = sorted(counts.items(), key=lambda item: item[1], reverse=True)

    rated = []
    for display_name, entry in collected:
        raw_rating = entry.get("letterboxd_memberrating")
        if not raw_rating:
            continue
        try:
            rating_value = float(raw_rating)
        except (TypeError, ValueError):
            continue
        rated.append((rating_value, display_name, entry))
    rated.sort(key=lambda item: item[0], reverse=True)
    top5 = rated[:5]

    lines = ["\U0001f4ca <b>Report settimanale</b>\n"]
    lines.append("<b>Film visti questa settimana</b>")
    for display_name, count in ranked_counts:
        lines.append(f"- {html.escape(display_name)}: {count} film")

    lines.append("")
    lines.append("<b>Top 5 per voto</b>")
    if not top5:
        lines.append("Nessun film votato questa settimana.")
    else:
        for i, (rating_value, display_name, entry) in enumerate(top5, start=1):
            film_title = entry.get("letterboxd_filmtitle") or entry.get("title", "Film")
            film_year = entry.get("letterboxd_filmyear")
            link = entry.get("link", "")
            title_part = html.escape(film_title)
            if film_year:
                title_part += f" ({html.escape(str(film_year))})"
            stars = stars_from_rating(rating_value)
            lines.append(
                f"{i}. <a href=\"{html.escape(link)}\">{title_part}</a> {stars} — {html.escape(display_name)}"
            )

    return "\n".join(lines)


def send_telegram_message(chat_id, text):
    resp = requests.post(
        TELEGRAM_API.format(token=TOKEN),
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"[error] Telegram API error {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        chats = get_active_chats()
        if not chats:
            print("[warn] no active chats known yet, skipping weekly report", file=sys.stderr)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "skipped": "no active chats"}')
            return

        since = datetime.now(timezone.utc) - timedelta(hours=WEEKLY_LOOKBACK_HOURS)
        ok = True
        for chat_id in chats:
            try:
                collected = collect_entries(chat_id, since)
                send_telegram_message(chat_id, build_weekly_report_text(collected))
            except Exception as exc:  # noqa: BLE001
                print(f"[error] failed to send weekly report to {chat_id}: {exc}", file=sys.stderr)
                ok = False

        self.send_response(200 if ok else 500)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}' if ok else b'{"ok": false}')
