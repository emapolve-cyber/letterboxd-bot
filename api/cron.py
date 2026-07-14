"""
Vercel serverless function: daily digest cron.
File path in the repo must be: api/cron.py

Triggered automatically once a day by Vercel Cron Jobs (see vercel.json).
Sends each "active chat" (every Telegram group where a bot command has
been used - see api/webhook.py) its OWN Letterboxd recap: a photo album
of movie posters (via TMDB, if configured) followed by the text digest,
based on that group's own independent list of tracked users.

Falls back to the TELEGRAM_CHAT_ID env var if no active chat has been
recorded yet (e.g. right after first deploy).
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
TELEGRAM_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"
TELEGRAM_MEDIA_GROUP_API = "https://api.telegram.org/bot{token}/sendMediaGroup"
MAX_MESSAGE_LEN = 4000
MAX_ALBUM_SIZE = 10

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
FALLBACK_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "24"))
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

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


def fetch_new_entries(username, since):
    url = RSS_URL.format(username=username)
    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        print(f"[warn] could not read feed for {username}: {feed.bozo_exception}", file=sys.stderr)
        return []

    new_entries = []
    seen = set()
    for entry in feed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published:
            continue
        published_dt = datetime.fromtimestamp(time.mktime(published), tz=timezone.utc)
        if published_dt < since:
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
        return ["Nessuna attività Letterboxd nelle ultime 24 ore."]

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


def tmdb_poster_url(title, year):
    if not TMDB_API_KEY:
        return None
    try:
        params = {"api_key": TMDB_API_KEY, "query": title}
        if year:
            params["year"] = year
        resp = requests.get(TMDB_SEARCH_URL, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        poster_path = results[0].get("poster_path")
        if not poster_path:
            return None
        return TMDB_IMAGE_BASE + poster_path
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] TMDB lookup failed for '{title}': {exc}", file=sys.stderr)
        return None


def collect_poster_urls(collected, limit=MAX_ALBUM_SIZE):
    urls = []
    for _display_name, entry in collected:
        if len(urls) >= limit:
            break
        film_title = entry.get("letterboxd_filmtitle")
        if not film_title:
            continue
        poster = tmdb_poster_url(film_title, entry.get("letterboxd_filmyear"))
        if poster:
            urls.append(poster)
    return urls


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


def send_photo_album(chat_id, urls):
    if not urls:
        return
    try:
        if len(urls) == 1:
            resp = requests.post(
                TELEGRAM_PHOTO_API.format(token=TOKEN),
                data={"chat_id": chat_id, "photo": urls[0]},
                timeout=30,
            )
        else:
            media = [{"type": "photo", "media": u} for u in urls[:MAX_ALBUM_SIZE]]
            resp = requests.post(
                TELEGRAM_MEDIA_GROUP_API.format(token=TOKEN),
                data={"chat_id": chat_id, "media": json.dumps(media)},
                timeout=30,
            )
        if not resp.ok:
            print(f"[warn] failed to send poster album: {resp.status_code} {resp.text}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] failed to send poster album: {exc}", file=sys.stderr)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        chats = get_active_chats()
        if not chats:
            print("[warn] no active chats known yet, skipping digest", file=sys.stderr)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "skipped": "no active chats"}')
            return

        since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        ok = True
        for chat_id in chats:
            try:
                collected = collect_entries(chat_id, since)
                poster_urls = collect_poster_urls(collected)
                if poster_urls:
                    send_photo_album(chat_id, poster_urls)
                for msg in build_text_messages(collected, "Letterboxd - riepilogo giornaliero"):
                    send_telegram_message(chat_id, msg)
            except Exception as exc:  # noqa: BLE001
                print(f"[error] failed to send digest to {chat_id}: {exc}", file=sys.stderr)
                ok = False

        self.send_response(200 if ok else 500)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}' if ok else b'{"ok": false}')
