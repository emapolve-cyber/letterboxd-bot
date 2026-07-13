"""
Vercel serverless function: Telegram webhook.
File path in the repo must be: api/webhook.py

Handles incoming Telegram updates:
  - "/update" (or "@bot update")      -> sends this group's daily recap now
  - "/top"                            -> sends this group's weekly report
                                          (films watched per user + top 5
                                          by rating) for the last 7 days
  - "/aggiungi Nome Cognome username" -> adds a user to THIS group's list
  - "/rimuovi username"               -> removes a user from THIS group
  - "/lista"                          -> lists this group's tracked users
  - "/stop"                           -> stops the daily digest in this chat
  - "/help"                           -> shows available commands

The bot can be added to any number of Telegram groups at the same time.
Each group tracks its own independent list of Letterboxd users (stored in
Upstash Redis, keyed by chat id). The first time a recognized command is
used in a group, that group is registered as an "active chat" and starts
receiving its own daily digest (api/cron.py) and weekly report
(api/weekly.py). Use /stop to unregister.

If TMDB_API_KEY is configured, the daily digest also sends a photo album
with movie posters (via TMDB) before the text recap.
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

# ---- config -------------------------------------------------------------

RSS_URL = "https://letterboxd.com/{username}/rss/"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"
TELEGRAM_MEDIA_GROUP_API = "https://api.telegram.org/bot{token}/sendMediaGroup"
MAX_MESSAGE_LEN = 4000
MAX_ALBUM_SIZE = 10
WEEKLY_LOOKBACK_HOURS = 24 * 7

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@").lower()
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "24"))
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

REDIS_URL = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
REDIS_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]
USERS_KEY_PREFIX = "letterboxd:users:"
ACTIVE_CHATS_KEY = "letterboxd:active_chats"

# The very first group this bot was set up in - it keeps its original
# 8-user list automatically. Any other, new group starts empty.
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

# ---- Upstash Redis (tiny REST-based key/value store) --------------------


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


def save_users(chat_id, users):
    key = f"{USERS_KEY_PREFIX}{chat_id}"
    redis_set(key, json.dumps(users))


def get_active_chats():
    raw = redis_get(ACTIVE_CHATS_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def add_active_chat(chat_id):
    try:
        chats = get_active_chats()
        cid = str(chat_id)
        if cid not in chats:
            chats.append(cid)
            redis_set(ACTIVE_CHATS_KEY, json.dumps(chats))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] failed to register active chat: {exc}", file=sys.stderr)


def remove_active_chat(chat_id):
    try:
        chats = get_active_chats()
        cid = str(chat_id)
        if cid in chats:
            chats.remove(cid)
            redis_set(ACTIVE_CHATS_KEY, json.dumps(chats))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] failed to unregister active chat: {exc}", file=sys.stderr)


# ---- Letterboxd feed logic ------------------------------------------


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


def collect_entries(chat_id, since):
    """Returns a list of (display_name, entry) tuples, oldest first."""
    users = get_users(chat_id)
    collected = []
    for display_name, username in users.items():
        for entry in fetch_new_entries(username, since):
            collected.append((display_name, entry))
    return collected


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


def build_text_messages(collected, header_text):
    if not collected:
        return ["Nessuna attività Letterboxd nel periodo richiesto."]

    seen_order = []
    grouped = {}
    for display_name, entry in collected:
        grouped.setdefault(display_name, []).append(entry)
        if display_name not in seen_order:
            seen_order.append(display_name)

    blocks = []
    for display_name in seen_order:
        lines = [format_entry(e) for e in grouped[display_name]]
        block = f"<b>{html.escape(display_name)}</b>\n" + "\n".join(lines)
        blocks.append(block)

    header = f"\U0001f3a5 <b>{html.escape(header_text)}</b>\n\n"
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
        plural = "film" if count == 1 else "film"
        lines.append(f"- {html.escape(display_name)}: {count} {plural}")

    lines.append("")
