#!/usr/bin/env python3
"""Send a daily historical-event notification from GitHub Actions."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from zoneinfo import ZoneInfo


WIKIMEDIA_ON_THIS_DAY = "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/{month}/{day}"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"
GITHUB_API_URL = "https://api.github.com"
TIMEZONE = ZoneInfo("America/New_York")
SPOTIFY_ERRORS = (RuntimeError, urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError)
TITLE_STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "in",
    "of",
    "on",
    "or",
    "part",
    "the",
    "to",
}


@dataclass(frozen=True)
class HistoricalEvent:
    year: int
    text: str
    title: str
    wikipedia_url: str


@dataclass(frozen=True)
class Podcast:
    name: str
    publisher: str
    url: str
    description: str


def main() -> None:
    force_send = os.getenv("FORCE_SEND", "").lower() in {"1", "true", "yes"}
    now = dt.datetime.now(TIMEZONE)

    if not force_send and now.hour != 15:
        print(f"Skipping: current America/New_York time is {now:%H:%M}, not 15:00.")
        return

    month, day = target_month_day(now)
    event = pick_event(fetch_events(month, day), month, day)
    podcasts = search_spotify_podcasts(event)

    title = f"On this day: {event.title} ({event.year})"
    body = render_notification(event, podcasts, month, day)

    webhook_url = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
    if webhook_url:
        post_webhook(webhook_url, title, body)
        print("Sent webhook notification.")
        return

    if create_github_issue(title, body):
        print("Created GitHub issue notification.")
    else:
        print("Printed notification because GitHub credentials are not configured.")


def target_month_day(now: dt.datetime) -> tuple[int, int]:
    override = os.getenv("HISTORY_DATE", "").strip()
    if not override:
        return now.month, now.day

    try:
        parsed = dt.datetime.strptime(override, "%m-%d")
    except ValueError as exc:
        raise SystemExit("HISTORY_DATE must use MM-DD, for example 07-20.") from exc

    return parsed.month, parsed.day


def fetch_events(month: int, day: int) -> list[dict]:
    url = WIKIMEDIA_ON_THIS_DAY.format(month=f"{month:02d}", day=f"{day:02d}")
    payload = request_json(
        urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "daily-history-notifier/1.0 (GitHub Actions)",
            },
        )
    )
    events = payload.get("events", [])
    if not events:
        raise RuntimeError(f"No Wikimedia events returned for {month:02d}-{day:02d}.")
    return events


def pick_event(events: list[dict], month: int, day: int) -> HistoricalEvent:
    scored_events = sorted(
        ((score_event(event), event) for event in events if event.get("pages")),
        key=lambda item: item[0],
    )
    best_events = [event for _, event in scored_events[-8:]]
    seed = int(hashlib.sha256(f"{month:02d}-{day:02d}".encode()).hexdigest(), 16)
    selected = random.Random(seed).choice(best_events)

    event_text = str(selected.get("text", "")).strip()
    page = choose_wikipedia_page(selected)
    urls = page.get("content_urls", {})
    desktop_url = urls.get("desktop", {}).get("page")
    wikipedia_url = desktop_url or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(page.get('title', ''))}"
    title = page.get("normalizedtitle") or page.get("title", "").replace("_", " ")

    return HistoricalEvent(
        year=int(selected.get("year", 0)),
        text=event_text,
        title=title,
        wikipedia_url=wikipedia_url,
    )


def choose_wikipedia_page(event: dict) -> dict:
    pages = event.get("pages", [])
    if not pages:
        return {}

    event_text = str(event.get("text", ""))
    specific_text = event_text.split(":", 1)[-1]
    event_terms = important_terms(specific_text)
    return max(pages, key=lambda page: score_page_match(page, event_terms, specific_text))


def score_page_match(page: dict, event_terms: set[str], event_text: str) -> tuple[int, int, int]:
    title = page.get("normalizedtitle") or page.get("title", "")
    normalized_title = str(title).lower().replace("_", " ")
    title_terms = important_terms(normalized_title)
    overlap_score = len(title_terms & event_terms)
    exact_title_score = int(normalized_title in event_text.lower())
    specificity_score = min(len(title_terms), 8)
    return overlap_score, exact_title_score, specificity_score


def important_terms(value: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9]+", value.lower())
        if len(term) > 2 and term not in TITLE_STOP_WORDS
    }


def score_event(event: dict) -> tuple[int, int]:
    text = str(event.get("text", "")).lower()
    pages = event.get("pages", [])
    first_page = pages[0] if pages else {}
    extract = str(first_page.get("extract", "")).lower()
    combined = f"{text} {extract}"

    keywords = {
        "battle": 9,
        "war": 8,
        "revolution": 8,
        "discovered": 7,
        "founded": 6,
        "first": 6,
        "independence": 6,
        "launched": 6,
        "invented": 6,
        "treaty": 6,
        "assassinated": 5,
        "space": 5,
        "election": 4,
        "protest": 4,
        "music": 4,
        "film": 3,
    }
    keyword_score = sum(weight for word, weight in keywords.items() if word in combined)
    page_score = min(len(pages), 8)
    text_score = min(len(text) // 60, 6)
    return keyword_score + page_score + text_score, int(event.get("year", 0))


def search_spotify_podcasts(event: HistoricalEvent) -> list[Podcast]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return []

    try:
        token = spotify_access_token(client_id, client_secret)
    except SPOTIFY_ERRORS as exc:
        print(f"Skipping Spotify podcast search: {exc}")
        return []

    query = textwrap.shorten(
        f"{event.title} {event.text} history podcast",
        width=180,
        placeholder="",
    )
    params = urllib.parse.urlencode(
        {
            "q": query,
            "type": "show",
            "market": "US",
            "limit": "5",
        }
    )
    try:
        payload = request_json(
            urllib.request.Request(
                f"{SPOTIFY_SEARCH_URL}?{params}",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
    except SPOTIFY_ERRORS as exc:
        print(f"Skipping Spotify podcast search: {exc}")
        return []

    shows = payload.get("shows", {}).get("items", [])
    return [podcast_from_show(show) for show in shows if show.get("external_urls", {}).get("spotify")][:5]


def spotify_access_token(client_id: str, client_secret: str) -> str:
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    payload = request_json(
        urllib.request.Request(
            SPOTIFY_TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
    )
    return payload["access_token"]


def podcast_from_show(show: dict) -> Podcast:
    return Podcast(
        name=show.get("name", "Untitled show"),
        publisher=show.get("publisher", "Unknown publisher"),
        url=show.get("external_urls", {}).get("spotify", ""),
        description=clean_text(show.get("description", "")),
    )


def render_notification(event: HistoricalEvent, podcasts: list[Podcast], month: int, day: int) -> str:
    blurb = event.text
    if len(blurb) > 360:
        blurb = textwrap.shorten(blurb, width=360, placeholder="...")

    lines = [
        f"## {event.title} ({event.year})",
        "",
        f"On {month:02d}-{day:02d}, {blurb}",
        "",
        f"Wikipedia: {event.wikipedia_url}",
    ]

    if podcasts:
        lines.extend(["", "### Spotify podcast matches"])
        for podcast in podcasts:
            description = textwrap.shorten(podcast.description, width=180, placeholder="...")
            detail = f" - {description}" if description else ""
            lines.append(f"- [{podcast.name}]({podcast.url}) by {podcast.publisher}{detail}")
    else:
        lines.extend(
            [
                "",
                "Spotify podcast matches were skipped because Spotify credentials are not configured.",
            ]
        )

    lines.extend(
        [
            "",
            "_Generated by the daily history notifier._",
        ]
    )
    return "\n".join(lines)


def create_github_issue(title: str, body: str) -> bool:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not token or not repository:
        print(title)
        print()
        print(body)
        return False

    owner, repo = repository.split("/", 1)
    request_json(
        urllib.request.Request(
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/issues",
            data=json.dumps({"title": title, "body": body, "labels": ["daily-history"]}).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "daily-history-notifier/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
    )
    return True


def post_webhook(webhook_url: str, title: str, body: str) -> None:
    message = f"{title}\n\n{body}"
    if "discord.com/api/webhooks/" in webhook_url or "discordapp.com/api/webhooks/" in webhook_url:
        payload = {"content": truncate_text(message, limit=2000)}
    else:
        payload = {"text": message}

    request_json(
        urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ),
        expect_json=False,
    )


def request_json(request: urllib.request.Request, expect_json: bool = True) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {request.full_url}: {details}") from exc

    if not expect_json:
        return {}
    return json.loads(data.decode("utf-8"))


def clean_text(value: str) -> str:
    return " ".join(value.replace("<br/>", " ").replace("<br>", " ").split())


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


if __name__ == "__main__":
    main()
