"""
Vercel serverless function: Telegram webhook.
File path in the repo must be: api/webhook.py

Handles incoming Telegram updates:
  - "/update" (or "@bot update")      -> sends the Letterboxd recap now,
                                          only to the chat that asked
  - "/aggiungi Nome Cognome username" -> adds a user to track (shared
                                          across all groups)
  - "/rimuovi username"               -> removes a tracked user
  - "/lista"                          -> lists tracked users
  - "/stop"                           -> stops the daily digest in this chat
  - "/help"                           -> shows available commands

The bot can be added to any number of Telegram groups at the same time.
The first time a recognized command is used in a group, that group is
registered in Upstash Redis as an "active chat" and will start receiving
the daily digest (api/cron.py) automatically. Use /stop to unregister a
group. The tracked-user list is shared across all groups.
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
MAX_MESSAGE_LEN = 4000

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@").lower()
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "24"))

REDIS_URL = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
REDIS_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]
USERS_KEY = "letterboxd:users"
ACTIVE_CHATS_KEY = "letterboxd:active_chats"

# Seed list used only the very first time (when Redis has no data yet).
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


def get_users():
    raw = redis_get(USERS_KEY)
    if raw is None:
        redis_set(USERS_KEY, json.dumps(DEFAULT_USERS))
        return dict(DEFAULT_USERS)
    return json.loads(raw)


def save_users(users):
    redis_set(USERS_KEY, json.dumps(users))


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


# ---- Letterboxd / digest logic ------------------------------------------


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
        return ["Nessuna attività Letterboxd nel periodo richiesto."]

    header = "\U0001f3a5 <b>Letterboxd - riepilogo</b>\n\n"
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


def run_and_send_digest(chat_id):
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    for msg in build_message(since):
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
    "/update - manda subito il riepilogo in questo gruppo\n"
    "/lista - mostra gli utenti monitorati\n"
    "/aggiungi Nome Cognome usernameletterboxd - aggiungi un utente\n"
    "/rimuovi usernameletterboxd - rimuovi un utente\n"
    "/stop - disattiva il digest giornaliero in questo gruppo\n"
    "/help - mostra questo messaggio\n\n"
    "Il bot può stare in più gruppi contemporaneamente: usare un comando "
    "in un gruppo lo attiva automaticamente per il digest giornaliero."
)


def handle_command(text):
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
        users = get_users()
        if not users:
            return "Nessun utente configurato."
        lines = [f"- {html.escape(name)} → {html.escape(uname)}" for name, uname in users.items()]
        return "<b>Utenti monitorati:</b>\n" + "\n".join(lines)

    if lower.startswith("/aggiungi"):
        parts = stripped.split()
        if len(parts) < 3:
            return "Uso corretto: /aggiungi Nome Cognome usernameletterboxd"
        username = parts[-1]
        display_name = " ".join(parts[1:-1])
        users = get_users()
        users[display_name] = username
        save_users(users)
        return f"Aggiunto: {html.escape(display_name)} → {html.escape(username)}"

    if lower.startswith("/rimuovi"):
        parts = stripped.split()
        if len(parts) != 2:
            return "Uso corretto: /rimuovi usernameletterboxd"
        target = parts[1].lower()
        users = get_users()
        to_delete = [name for name, uname in users.items() if uname.lower() == target]
        if not to_delete:
            return f"Nessun utente trovato con username '{html.escape(parts[1])}'"
        for name in to_delete:
            del users[name]
        save_users(users)
        return f"Rimosso: {html.escape(', '.join(to_delete))}"

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
                reply = handle_command(text)
                if reply is not None:
                    add_active_chat(chat_id)
                    send_telegram_message(chat_id, reply)
                elif is_update_command(text):
                    add_active_chat(chat_id)
                    run_and_send_digest(chat_id)
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
