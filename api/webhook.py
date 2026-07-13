"""
Vercel serverless function: Telegram webhook.
File path in the repo must be: api/webhook.py

Handles incoming Telegram updates:
  - "/update" (or "@bot update")      -> sends this group's recap now
  - "/aggiungi Nome Cognome username" -> adds a user to THIS group's list
  - "/rimuovi username"               -> removes a user from THIS group
  - "/lista"                          -> lists this group's tracked users
  - "/stop"                           -> stops the daily digest in this chat
  - "/help"                           -> shows available commands

The bot can be added to any number of Telegram groups at the same time.
Each group tracks its own independent list of Letterboxd users (stored in
Upstash Redis, keyed by chat id). The first time a recognized command is
used in a group, that group is registered as an "active chat" and starts
receiving its own daily digest (api/cron.py). Use /stop to unregister.

If TMDB_API_KEY is configured, the digest also sends a photo album with
movie posters (via TMDB) before the text recap.
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


# ---- TMDB poster lookup ---------------------------------------------


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


# ---- Telegram sending -------------------------------------------------


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


def run_and_send_digest(chat_id, header_text="Letterboxd - riepilogo"):
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    collected = collect_entries(chat_id, since)

    poster_urls = collect_poster_urls(collected)
    if poster_urls:
        send_photo_album(chat_id, poster_urls)

    for msg in build_text_messages(collected, header_text):
        send_telegram_message(chat_id, msg)


def is_update_command(text):
    if not text:
        return False
    text = text.strip().lower()
    if text.startswith("/update"):
        return True
    if BOT_USERNAME and f"@{BOT_USERNAME}" in text and "update" in text:
        return True
    return False


def is_stop_command(text):
    if not text:
        return False
    return text.strip().lower().startswith("/stop")


# ---- user-management commands --------------------------------------------

HELP_TEXT = (
    "<b>Comandi disponibili</b>\n"
    "/update - manda subito il riepilogo di questo gruppo\n"
    "/lista - mostra gli utenti monitorati in questo gruppo\n"
    "/aggiungi Nome Cognome usernameletterboxd - aggiungi un utente a questo gruppo\n"
    "/rimuovi usernameletterboxd - rimuovi un utente da questo gruppo\n"
    "/stop - disattiva il digest giornaliero in questo gruppo\n"
    "/help - mostra questo messaggio\n\n"
    "Ogni gruppo ha la propria lista di utenti Letterboxd, indipendente dagli altri."
)


def handle_command(text, chat_id):
    """Returns a reply string if this text was a recognized management
    command, otherwise None (so the caller can fall through to other
    handling, e.g. is_update_command / is_stop_command)."""
    if not text:
        return None
    stripped = text.strip()
    lower = stripped.lower()

    if lower.startswith("/help"):
        return HELP_TEXT

    if lower.startswith("/lista"):
        users = get_users(chat_id)
        if not users:
            return "Nessun utente configurato in questo gruppo. Usa /aggiungi per iniziare."
        lines = [f"- {html.escape(name)} → {html.escape(uname)}" for name, uname in users.items()]
        return "<b>Utenti monitorati in questo gruppo:</b>\n" + "\n".join(lines)

    if lower.startswith("/aggiungi"):
        parts = stripped.split()
        if len(parts) < 3:
            return "Uso corretto: /aggiungi Nome Cognome usernameletterboxd"
        username = parts[-1]
        display_name = " ".join(parts[1:-1])
        users = get_users(chat_id)
        users[display_name] = username
        save_users(chat_id, users)
        return f"Aggiunto a questo gruppo: {html.escape(display_name)} → {html.escape(username)}"

    if lower.startswith("/rimuovi"):
        parts = stripped.split()
        if len(parts) != 2:
            return "Uso corretto: /rimuovi usernameletterboxd"
        target = parts[1].lower()
        users = get_users(chat_id)
        to_delete = [name for name, uname in users.items() if uname.lower() == target]
        if not to_delete:
            return f"Nessun utente trovato con username '{html.escape(parts[1])}' in questo gruppo"
        for name in to_delete:
            del users[name]
        save_users(chat_id, users)
        return f"Rimosso da questo gruppo: {html.escape(', '.join(to_delete))}"

    return None


# ---- Vercel entrypoint -------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            update = json.loads(body or b"{}")
        except json.JSONDecodeError:
            update = {}

        message = update.get("message") or update.get("channel_post") or {}
        text = message.get("text", "")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        try:
            if chat_id is None:
                pass
            elif is_stop_command(text):
                remove_active_chat(chat_id)
                send_telegram_message(
                    chat_id,
                    "Digest giornaliero disattivato in questo gruppo. "
                    "Gli altri comandi restano comunque disponibili.",
                )
            else:
                reply = handle_command(text, chat_id)
                if reply is not None:
                    add_active_chat(chat_id)
                    send_telegram_message(chat_id, reply)
                elif is_update_command(text):
                    add_active_chat(chat_id)
                    run_and_send_digest(chat_id, header_text="Letterboxd - riepilogo")
        except Exception as exc:  # noqa: BLE001
            print(f"[error] failed to handle message: {exc}", file=sys.stderr)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        # simple health check
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"webhook alive")
