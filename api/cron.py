"""
Vercel serverless function: daily digest cron.
File path in the repo must be: api/cron.py

Triggered automatically once a day by Vercel Cron Jobs (see vercel.json).
Sends the Letterboxd recap of the last LOOKBACK_HOURS hours to the chat.

The list of tracked users is read from Upstash Redis (shared with
api/webhook.py, which is where users get added/removed via Telegram
commands).
"""

import os
import sys
import json
import html
import time
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta

import feedparser
import requests

RSS_URL = "https://letterboxd.com/{username}/rss/"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LEN = 4000

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "24"))

REDIS_URL = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
REDIS_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]
USERS_KEY = "letterboxd:users"

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


def get_users():
    raw = redis_get(USERS_KEY)
    if raw is None:
        redis_set(USERS_KEY, json.dumps(DEFAULT_USERS))
        return dict(DEFAULT_USERS)
    return json.loads(raw)


def stars_from_rating(rating):
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        return ""
    full = int(rating)
    half = rating - full >= 0.5
    return "★" * full + ("½" if half else "")


def fetch_new_entries(username, since):
    url = RSS_URL.format(username=username)
    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        print(f"[warn] could not read feed for {username}: {feed.bozo_exception}", file=sys.stderr)
        return []

    new_entries = []
    for entry in feed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published:
            continue
        published_dt = datetime.fromtimestamp(time.mktime(published), tz=timezone.utc)
        if published_dt < since:
            continue
        new_entries.append(entry)

    new_entries.reverse()
    return new_entries


def format_entry(entry):
    film_title = entry.get("letterboxd_filmtitle")
    film_year = entry.get("letterboxd_filmyear")
    rating = entry.get("letterboxd_memberrating")
    rewatch = entry.get("letterboxd_rewatch") == "Yes"
    link = entry.get("link", "")

    stars = stars_from_rating(rating)
    rewatch_icon = " \U0001f501" if rewatch else ""

    if film_title:
        title_part = html.escape(film_title)
        if film_year:
            title_part += f" ({html.escape(str(film_year))})"
        line = f"\U0001f3ac <a href=\"{html.escape(link)}\">{title_part}</a>"
        if stars:
            line += f" {stars}"
        line += rewatch_icon
    else:
        title_part = html.escape(entry.get("title", "Nuovo contenuto"))
        line = f"\U0001f4dd <a href=\"{html.escape(link)}\">{title_part}</a>"

    return line


def build_message(since):
    users = get_users()
    blocks = []
    for display_name, username in users.items():
        entries = fetch_new_entries(username, since)
        if not entries:
            continue
        lines = [format_entry(e) for e in entries]
        block = f"<b>{html.escape(display_name)}</b>\n" + "\n".join(lines)
        blocks.append(block)

    if not blocks:
        return ["Nessuna attività Letterboxd nelle ultime 24 ore."]

    header = "\U0001f3a5 <b>Letterboxd - riepilogo giornaliero</b>\n\n"
    full_text = header + "\n\n".join(blocks)

    if len(full_text) <= MAX_MESSAGE_LEN:
        return [full_text]

    chunks = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = ""
        current += block + "\n\n"
    if current.strip():
        chunks.append(current)
    return chunks


def send_telegram_message(text):
    resp = requests.post(
        TELEGRAM_API.format(token=TOKEN),
        data={
            "chat_id": CHAT_ID,
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
        since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        try:
            for msg in build_message(since):
                send_telegram_message(msg)
            ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"[error] daily digest failed: {exc}", file=sys.stderr)
            ok = False

        self.send_response(200 if ok else 500)
        self.send_header("Content-Type",
