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
import re
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
from bs4 import BeautifulSoup

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


def _parse_feed_entries(username):
    """Fetch and dedupe every entry currently in a user's Letterboxd RSS
    feed, newest first. Letterboxd's RSS only exposes the user's most
    recent activity (roughly their last 50-100 diary entries) - this is
    NOT their full watch history.
    """
    url = RSS_URL.format(username=username)
    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        print(f"[warn] could not read feed for {username}: {feed.bozo_exception}", file=sys.stderr)
        return []

    entries = []
    seen = set()
    for entry in feed.entries:
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
        entries.append(entry)

    return entries


def fetch_new_entries(username, since):
    """Entries watched/published on or after `since`, oldest first."""
    matched = []
    for entry in _parse_feed_entries(username):
        entry_dt = _entry_date_utc(entry)
        if not entry_dt or entry_dt < since:
            continue
        matched.append(entry)
    matched.reverse()
    return matched


def fetch_all_entries(username):
    """Every entry currently available in the user's feed, oldest first.
    See _parse_feed_entries: this is recent activity, not full history.
    """
    entries = _parse_feed_entries(username)
    entries.reverse()
    return entries


# ---- Letterboxd diary scraping (year-scoped) -------------------------
#
# The RSS feed only exposes a user's most recent ~50-100 diary entries.
# For commands that need a broader (but still bounded) view, we scrape
# https://letterboxd.com/{username}/diary/films/for/{year}/ instead.
# Scoping to a single year keeps this fast and reasonable (a handful of
# pages per user) - scraping a user's *entire* history was tested and
# ruled out: some users have 50+ pages of diary entries, which would be
# far too slow and too aggressive towards Letterboxd's servers.
#
# Returned entries are plain dicts using the same keys as RSS entries
# (letterboxd_filmtitle, letterboxd_filmyear, letterboxd_memberrating,
# letterboxd_rewatch, letterboxd_memberlike, letterboxd_watcheddate,
# link) so they can be passed into the exact same formatting helpers
# (stars_from_rating, etc.) used elsewhere for RSS entries.

DIARY_YEAR_URL = "https://letterboxd.com/{username}/diary/films/for/{year}/page/{page}/"
DIARY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DIARY_MAX_PAGES = 8  # safety cap per user, per year


def _parse_diary_row(row, username):
    title_link = row.select_one("h2.primaryname a")
    if not title_link:
        return None
    film_title = title_link.get_text(strip=True)

    year_link = row.select_one("span.releasedate a")
    film_year = year_link.get_text(strip=True) if year_link else None

    href = title_link.get("href", "")
    link = f"https://letterboxd.com{href}" if href.startswith("/") else href

    day_link = row.select_one("td.col-daydate a.daydate")
    watched_date = None
    if day_link:
        day_href = day_link.get("href", "")
        m = re.search(r"/for/(\d{4})/(\d{2})/(\d{2})/", day_href)
        if m:
            watched_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    rating_span = row.select_one("td.col-rating span.rating")
    rating = None
    if rating_span:
        classes = rating_span.get("class", [])
        for cls in classes:
            m = re.match(r"rated-(\d+)", cls)
            if m:
                rating = int(m.group(1)) / 2  # 0-10 scale -> 0-5 stars
                break

    liked = row.select_one("td.col-like span.icon-liked") is not None

    rewatch_td = row.select_one("td.col-rewatch")
    rewatch = bool(rewatch_td) and "icon-status-off" not in rewatch_td.get("class", [])

    return {
        "letterboxd_filmtitle": film_title,
        "letterboxd_filmyear": film_year,
        "letterboxd_memberrating": str(rating) if rating is not None else None,
        "letterboxd_rewatch": "Yes" if rewatch else "No",
        "letterboxd_memberlike": "Yes" if liked else "No",
        "letterboxd_watcheddate": watched_date,
        "link": link,
    }


def fetch_diary_year_entries(username, year, max_pages=DIARY_MAX_PAGES):
    """Scrape a user's Letterboxd diary for a single year, oldest first."""
    entries = []
    for page in range(1, max_pages + 1):
        url = DIARY_YEAR_URL.format(username=username, year=year, page=page)
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": DIARY_USER_AGENT},
                timeout=15,
            )
        except requests.RequestException as exc:
            print(f"[warn] diary scrape failed for {username} p{page}: {exc}", file=sys.stderr)
            break

        if not resp.ok:
            print(f"[warn] diary scrape HTTP {resp.status_code} for {username} p{page}", file=sys.stderr)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr.diary-entry-row")
        if not rows:
            break

        for row in rows:
            parsed = _parse_diary_row(row, username)
            if parsed:
                entries.append(parsed)

        # A short page (fewer rows than a full page) means we've reached
        # the end of this year's diary - no need to request further pages.
        if len(rows) < 25:
            break

    entries.reverse()
    return entries


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
    liked = entry.get("letterboxd_memberlike") == "Yes"
    link = entry.get("link", "")

    stars = stars_from_rating(rating)
    heart = " ❤️" if liked else ""
    rewatch_icon = " \U0001f501" if rewatch else ""

    if film_title:
        title_part = html.escape(film_title)
        if film_year:
            title_part += f" ({html.escape(str(film_year))})"
        line = f"\U0001f3ac <a href=\"{html.escape(link)}\">{title_part}</a>"
        if stars:
            line += f" {stars}"
        line += heart
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
    # Only count actual watched films. The RSS feed also contains other
    # activity (e.g. ranked list updates like "Il mio Miyazaki"), which
    # has no letterboxd:filmTitle and isn't a watched film - it shouldn't
    # be counted towards "film visti questa settimana".
    collected = [(name, entry) for name, entry in collected if entry.get("letterboxd_filmtitle")]

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
            heart = " ❤️" if entry.get("letterboxd_memberlike") == "Yes" else ""
            lines.append(
                f"{i}. <a href=\"{html.escape(link)}\">{title_part}</a> {stars}{heart} — {html.escape(display_name)}"
            )

    return "\n".join(lines)


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


def run_and_send_weekly_report(chat_id):
    since = datetime.now(timezone.utc) - timedelta(hours=WEEKLY_LOOKBACK_HOURS)
    collected = collect_entries(chat_id, since)
    send_telegram_message(chat_id, build_weekly_report_text(collected))


def is_update_command(text):
    if not text:
        return False
    text = text.strip().lower()
    if text.startswith("/update"):
        return True
    if BOT_USERNAME and f"@{BOT_USERNAME}" in text and "update" in text:
        return True
    return False


def is_top_command(text):
    if not text:
        return False
    return text.strip().lower().startswith("/top")


def is_stop_command(text):
    if not text:
        return False
    return text.strip().lower().startswith("/stop")


# ---- user-management commands --------------------------------------------

HELP_TEXT = (
    "<b>Comandi disponibili</b>\n"
    "/update - manda subito il riepilogo di questo gruppo\n"
    "/top - report settimanale: film visti per utente e top 5 per voto\n"
    "/lista - mostra gli utenti monitorati in questo gruppo\n"
    "/aggiungi Nome Cognome usernameletterboxd - aggiungi un utente a questo gruppo\n"
    "/rimuovi usernameletterboxd - rimuovi un utente da questo gruppo\n"
    "/film titolo - mostra chi nel gruppo ha visto un film quest'anno e con che voto\n"
    "/sfida nome1 nome2 [giorni] - confronta quanti film hanno visto due utenti (default 7 giorni)\n"
    "/stats - statistiche di gruppo (film totali, utente più attivo, voto medio, ecc.)\n"
    "/stop - disattiva il digest giornaliero in questo gruppo\n"
    "/help - mostra questo messaggio\n\n"
    "Ogni gruppo ha la propria lista di utenti Letterboxd, indipendente dagli altri.\n"
    "Nota: /film guarda il diario dell'anno corrente di ciascun utente (scraping mirato, "
    "non l'intero storico). /sfida e /stats usano invece le attività più recenti "
    "disponibili nel feed RSS di ciascun utente."
)


def handle_command(text, chat_id):
    """Returns a reply string if this text was a recognized management
    command, otherwise None (so the caller can fall through to other
    handling, e.g. is_update_command / is_top_command / is_stop_command)."""
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

    if lower.startswith("/film"):
        query = stripped[len("/film"):].strip()
        if not query:
            return "Uso corretto: /film titolo del film"
        users = get_users(chat_id)
        if not users:
            return "Nessun utente configurato in questo gruppo. Usa /aggiungi per iniziare."
        query_lower = query.lower()
        current_year = datetime.now(timezone.utc).year
        matches = []
        for display_name, username in users.items():
            for entry in fetch_diary_year_entries(username, current_year):
                title = entry.get("letterboxd_filmtitle")
                if title and query_lower in title.lower():
                    matches.append((display_name, entry))

        if not matches:
            return (
                f"Nessuno ha visto un film che corrisponde a '{html.escape(query)}' "
                f"nel diario {current_year} di questo gruppo."
            )

        matches.sort(
            key=lambda item: _entry_date_utc(item[1]) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        lines = [f"<b>\U0001f3ac Risultati per \"{html.escape(query)}\"</b>\n"]
        for display_name, entry in matches:
            film_title = entry.get("letterboxd_filmtitle")
            film_year = entry.get("letterboxd_filmyear")
            rating = entry.get("letterboxd_memberrating")
            link = entry.get("link", "")
            title_part = html.escape(film_title)
            if film_year:
                title_part += f" ({html.escape(str(film_year))})"
            stars = stars_from_rating(rating)
            heart = " ❤️" if entry.get("letterboxd_memberlike") == "Yes" else ""
            watched = entry.get("letterboxd_watcheddate", "")
            watched_part = f" ({html.escape(watched)})" if watched else ""
            lines.append(
                f"- <a href=\"{html.escape(link)}\">{title_part}</a> {stars}{heart} "
                f"— {html.escape(display_name)}{watched_part}"
            )
        return "\n".join(lines)

    if lower.startswith("/sfida"):
        tokens = stripped.split()[1:]
        if not tokens:
            return "Uso corretto: /sfida nome1 nome2 [giorni] (nomi come in /lista, es. /sfida Fefo Arturo)"

        days = 7
        if tokens[-1].isdigit():
            days = max(1, int(tokens[-1]))
            tokens = tokens[:-1]

        users = get_users(chat_id)
        # Display names can contain spaces (e.g. "Lorenzo Sartor"), so we
        # can't just split on whitespace into two names. Instead, try every
        # way of splitting the remaining tokens into two groups and check
        # both against the group's actual registered names.
        names_lower = {name.lower(): name for name in users}
        match = None
        for i in range(1, len(tokens)):
            name1 = " ".join(tokens[:i])
            name2 = " ".join(tokens[i:])
            if name1.lower() in names_lower and name2.lower() in names_lower:
                match = (names_lower[name1.lower()], names_lower[name2.lower()])
                break

        if not match:
            return (
                "Non ho riconosciuto due nomi validi. Uso corretto: /sfida nome1 nome2 [giorni] "
                "(nomi esatti come mostrati in /lista, es. /sfida Fefo Arturo)."
            )

        u1 = (match[0], users[match[0]])
        u2 = (match[1], users[match[1]])

        since = datetime.now(timezone.utc) - timedelta(days=days)
        films1 = [e for e in fetch_new_entries(u1[1], since) if e.get("letterboxd_filmtitle")]
        films2 = [e for e in fetch_new_entries(u2[1], since) if e.get("letterboxd_filmtitle")]
        n1, n2 = len(films1), len(films2)

        if n1 > n2:
            verdict = f"\U0001f3c6 {html.escape(u1[0])} vince!"
        elif n2 > n1:
            verdict = f"\U0001f3c6 {html.escape(u2[0])} vince!"
        else:
            verdict = "\U0001f91d Pareggio!"

        return "\n".join([
            f"<b>⚔️ Sfida: {html.escape(u1[0])} vs {html.escape(u2[0])}</b> (ultimi {days} giorni)\n",
            f"{html.escape(u1[0])}: {n1} film",
            f"{html.escape(u2[0])}: {n2} film",
            "",
            verdict,
        ])

    if lower.startswith("/stats"):
        users = get_users(chat_id)
        if not users:
            return "Nessun utente configurato in questo gruppo. Usa /aggiungi per iniziare."

        per_user = {}
        total = 0
        for display_name, username in users.items():
            entries = [e for e in fetch_all_entries(username) if e.get("letterboxd_filmtitle")]
            per_user[display_name] = entries
            total += len(entries)

        if total == 0:
            return "Nessun dato disponibile per calcolare le statistiche di gruppo."

        most_active = max(per_user.items(), key=lambda kv: len(kv[1]))

        ratings = []
        rewatches = 0
        likes = 0
        film_watchers = {}
        film_entry_for_key = {}
        rated_all = []
        for display_name, entries in per_user.items():
            for e in entries:
                r = e.get("letterboxd_memberrating")
                if r:
                    try:
                        rv = float(r)
                        ratings.append(rv)
                        rated_all.append((rv, display_name, e))
                    except (TypeError, ValueError):
                        pass
                if e.get("letterboxd_rewatch") == "Yes":
                    rewatches += 1
                if e.get("letterboxd_memberlike") == "Yes":
                    likes += 1
                key = (e.get("letterboxd_filmtitle"), e.get("letterboxd_filmyear"))
                film_watchers.setdefault(key, set()).add(display_name)
                film_entry_for_key.setdefault(key, e)

        avg_rating = sum(ratings) / len(ratings) if ratings else None
        most_shared_key, most_shared_watchers = max(film_watchers.items(), key=lambda kv: len(kv[1]))
        rated_all.sort(key=lambda item: item[0], reverse=True)

        lines = [
            "<b>\U0001f4c8 Statistiche di gruppo</b>",
            "<i>(basate sulle attività più recenti disponibili nel feed di ciascun utente, non sullo storico completo)</i>\n",
            f"Film totali registrati: {total}",
            f"Utente più attivo: {html.escape(most_active[0])} ({len(most_active[1])} film)",
        ]
        if avg_rating is not None:
            lines.append(f"Voto medio del gruppo: {avg_rating:.2f} ★")
        lines.append(f"Rewatch: {rewatches} ({rewatches / total:.0%})")
        lines.append(f"Film con cuore: {likes} ({likes / total:.0%})")

        if len(most_shared_watchers) > 1:
            title, year = most_shared_key
            e = film_entry_for_key[most_shared_key]
            link = e.get("link", "")
            title_part = html.escape(title or "?")
            if year:
                title_part += f" ({html.escape(str(year))})"
            watchers = ", ".join(html.escape(n) for n in sorted(most_shared_watchers))
            lines.append(
                f"Film più condiviso: <a href=\"{html.escape(link)}\">{title_part}</a> "
                f"— visto da {len(most_shared_watchers)} persone ({watchers})"
            )

        if rated_all:
            rating_value, display_name, e = rated_all[0]
            title = e.get("letterboxd_filmtitle")
            year = e.get("letterboxd_filmyear")
            link = e.get("link", "")
            title_part = html.escape(title)
            if year:
                title_part += f" ({html.escape(str(year))})"
            stars = stars_from_rating(rating_value)
            lines.append(
                f"Voto più alto: <a href=\"{html.escape(link)}\">{title_part}</a> {stars} — {html.escape(display_name)}"
            )

        return "\n".join(lines)

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
                elif is_top_command(text):
                    add_active_chat(chat_id)
                    run_and_send_weekly_report(chat_id)
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
