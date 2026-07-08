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
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
GITHUB_API_URL = "https://api.github.com"
TIMEZONE = ZoneInfo("America/New_York")
SPOTIFY_ERRORS = (RuntimeError, urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError)
OPENAI_ERRORS = (RuntimeError, urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError)
DEFAULT_EVENT_INTEREST_KEYWORDS = "war, crime, natural disasters, battles"
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
    podcasts, podcast_search_attempted = search_spotify_podcasts(event)

    title = f"On this day: {event.title} ({event.year})"
    body = render_notification(event, podcasts, month, day, podcast_search_attempted)

    webhook_url = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
    if webhook_url:
        try:
            post_webhook(webhook_url, title, body)
            print("Sent webhook notification.")
            return
        except RuntimeError as exc:
            print(f"Webhook notification failed: {exc}")
            print("Falling back to GitHub issue notification.")

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
    candidates = [event for event in events if event.get("pages")]
    if not candidates:
        raise RuntimeError(f"No Wikimedia events with pages returned for {month:02d}-{day:02d}.")

    selected = None
    if os.getenv("OPENAI_API_KEY", "").strip():
        try:
            selected = openai_pick_event(candidates, month, day)
            print(f"OpenAI selected historical event: {selected.get('year')} - {selected.get('text')}")
        except OPENAI_ERRORS as exc:
            print(f"OpenAI event selection failed: {exc}")
            print("Falling back to local historical event selection.")

    if selected is None:
        selected = locally_pick_event(candidates, month, day)

    return historical_event_from_wikimedia(selected)


def locally_pick_event(events: list[dict], month: int, day: int) -> dict:
    scored_events = sorted(
        ((score_event(event), event) for event in events),
        key=lambda item: item[0],
    )
    best_events = [event for _, event in scored_events[-8:]]
    seed = int(hashlib.sha256(f"{month:02d}-{day:02d}".encode()).hexdigest(), 16)
    return random.Random(seed).choice(best_events)


def openai_pick_event(events: list[dict], month: int, day: int) -> dict:
    payload = request_json(
        urllib.request.Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(openai_event_selection_request(events, month, day)).encode(),
            headers=openai_headers(),
            method="POST",
        )
    )
    selected_index = parse_openai_selected_index(payload)
    events_by_index = {index: event for index, event in enumerate(events)}
    if selected_index not in events_by_index:
        raise RuntimeError(f"OpenAI selected invalid event index: {selected_index}")
    return events_by_index[selected_index]


def openai_event_selection_request(events: list[dict], month: int, day: int) -> dict:
    interest_keywords = event_interest_keywords()
    candidates = [
        {
            "index": index,
            "year": event.get("year"),
            "text": str(event.get("text", "")),
            "page_titles": [
                page.get("normalizedtitle") or str(page.get("title", "")).replace("_", " ")
                for page in event.get("pages", [])[:5]
            ],
        }
        for index, event in enumerate(events)
    ]
    return {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "instructions": (
            "You choose one event for a daily history notification. "
            "Pick the single most interesting event for a general audience: specific, vivid, historically important, "
            "and likely to have good podcast or documentary context. Prefer concrete events over broad biographies. "
            "Give extra weight to events related to the user's interest keywords, but do not choose a weak event only "
            "because it matches a keyword. "
            "Return only the index of the chosen candidate."
        ),
        "input": json.dumps(
            {
                "date": f"{month:02d}-{day:02d}",
                "interest_keywords": interest_keywords,
                "candidates": candidates,
            }
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "selected_history_event",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "selected_index": {
                            "type": "integer",
                            "description": "The index of the most interesting event candidate.",
                        }
                    },
                    "required": ["selected_index"],
                },
            }
        },
    }


def event_interest_keywords() -> list[str]:
    raw_keywords = os.getenv("EVENT_INTEREST_KEYWORDS", DEFAULT_EVENT_INTEREST_KEYWORDS)
    return [keyword.strip() for keyword in raw_keywords.split(",") if keyword.strip()]


def parse_openai_selected_index(payload: dict) -> int:
    text = payload.get("output_text") or response_output_text(payload)
    parsed = json.loads(text)
    return int(parsed["selected_index"])


def historical_event_from_wikimedia(selected: dict) -> HistoricalEvent:
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

    if os.getenv("OPENAI_API_KEY", "").strip():
        try:
            return openai_choose_wikipedia_page(event)
        except OPENAI_ERRORS as exc:
            print(f"OpenAI Wikipedia page selection failed: {exc}")
            print("Falling back to local Wikipedia page scoring.")

    return locally_choose_wikipedia_page(event)


def locally_choose_wikipedia_page(event: dict) -> dict:
    pages = event.get("pages", [])
    event_text = str(event.get("text", ""))
    specific_text = event_text.split(":", 1)[-1]
    event_terms = important_terms(specific_text)
    return max(pages, key=lambda page: score_page_match(page, event_terms, specific_text))


def openai_choose_wikipedia_page(event: dict) -> dict:
    pages = event.get("pages", [])
    payload = request_json(
        urllib.request.Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(openai_page_selection_request(event)).encode(),
            headers=openai_headers(),
            method="POST",
        )
    )
    selected_index = parse_openai_selected_index(payload)
    pages_by_index = {index: page for index, page in enumerate(pages)}
    if selected_index not in pages_by_index:
        raise RuntimeError(f"OpenAI selected invalid Wikipedia page index: {selected_index}")
    return pages_by_index[selected_index]


def openai_page_selection_request(event: dict) -> dict:
    candidates = [
        {
            "index": index,
            "title": page.get("normalizedtitle") or str(page.get("title", "")).replace("_", " "),
            "extract": textwrap.shorten(str(page.get("extract", "")), width=450, placeholder="..."),
        }
        for index, page in enumerate(event.get("pages", [])[:10])
    ]
    return {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "instructions": (
            "Choose the single best Wikipedia page for the specific historical event. "
            "Prefer the page about the event's main subject, battle, siege, disaster, crime, or concrete incident. "
            "Avoid broad country, era, or generic biography pages when a more specific candidate explains the event. "
            "Return only the selected candidate index."
        ),
        "input": json.dumps(
            {
                "event": {
                    "year": event.get("year"),
                    "text": str(event.get("text", "")),
                },
                "candidates": candidates,
            }
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "selected_wikipedia_page",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "selected_index": {
                            "type": "integer",
                            "description": "The index of the best Wikipedia page candidate.",
                        }
                    },
                    "required": ["selected_index"],
                },
            }
        },
    }


def score_page_match(page: dict, event_terms: set[str], event_text: str) -> tuple[int, int, int, int, int]:
    title = page.get("normalizedtitle") or page.get("title", "")
    normalized_title = str(title).lower().replace("_", " ")
    normalized_event = event_text.lower()
    title_terms = important_terms(normalized_title)
    overlap_score = len(title_terms & event_terms)
    exact_title_score = int(contains_phrase(normalized_event, normalized_title))
    strong_context_pattern = rf"\b(?:battle|fortress|siege)\s+of\s+(?:the\s+)?{re.escape(normalized_title)}\b"
    strong_context_score = int(bool(re.search(strong_context_pattern, normalized_event)))
    generic_context_pattern = rf"\b(?:at|in|into|near|to|toward|towards)\s+{re.escape(normalized_title)}\b"
    generic_context_score = int(bool(re.search(generic_context_pattern, normalized_event)))
    possessive_penalty = int(bool(re.search(rf"\b{re.escape(normalized_title)}'s\b", normalized_event)))
    specificity_score = min(len(title_terms), 8)
    return (
        strong_context_score,
        exact_title_score,
        overlap_score,
        specificity_score - possessive_penalty,
        generic_context_score,
    )


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


def search_spotify_podcasts(event: HistoricalEvent) -> tuple[list[Podcast], bool]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return [], False

    try:
        token = spotify_access_token(client_id, client_secret)
    except SPOTIFY_ERRORS as exc:
        print(f"Skipping Spotify podcast search: {exc}")
        return [], True

    query = spotify_search_query(event)
    params = urllib.parse.urlencode(
        {
            "q": query,
            "type": "show",
            "market": "US",
            "limit": "10",
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
        return [], True

    shows = payload.get("shows", {}).get("items", [])
    podcasts = [
        podcast_from_show(show)
        for show in shows
        if show.get("external_urls", {}).get("spotify")
    ]
    debug_matching(f"Spotify query: {query}")
    debug_podcast_list("Spotify returned candidates", podcasts)
    if os.getenv("OPENAI_API_KEY", "").strip():
        try:
            return openai_rank_podcasts(event, podcasts), True
        except OPENAI_ERRORS as exc:
            print(f"OpenAI podcast ranking failed: {exc}")
            print("Falling back to local Spotify relevance scoring.")

    return locally_rank_podcasts(event, podcasts), True


def locally_rank_podcasts(event: HistoricalEvent, podcasts: list[Podcast]) -> list[Podcast]:
    ranked_podcasts = sorted(
        ((score_podcast_match(podcast, event), podcast) for podcast in podcasts),
        key=lambda item: item[0],
        reverse=True,
    )
    return [podcast for score, podcast in ranked_podcasts if score >= 2][:5]


def spotify_search_query(event: HistoricalEvent) -> str:
    return textwrap.shorten(f"{event.title} history podcast", width=120, placeholder="")


def openai_rank_podcasts(event: HistoricalEvent, podcasts: list[Podcast]) -> list[Podcast]:
    if not podcasts:
        return []

    payload = request_json(
        urllib.request.Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(openai_ranking_request(event, podcasts)).encode(),
            headers=openai_headers(),
            method="POST",
        )
    )
    selected_urls = parse_openai_selected_urls(payload)
    podcasts_by_url = {podcast.url: podcast for podcast in podcasts}
    selected = [podcasts_by_url[url] for url in selected_urls if url in podcasts_by_url][:5]
    debug_podcast_list("OpenAI selected Spotify candidates", selected)
    rejected = [podcast for podcast in podcasts if podcast.url not in {selected_podcast.url for selected_podcast in selected}]
    debug_podcast_list("OpenAI rejected Spotify candidates", rejected)
    return selected


def openai_ranking_request(event: HistoricalEvent, podcasts: list[Podcast]) -> dict:
    candidates = [
        {
            "name": podcast.name,
            "publisher": podcast.publisher,
            "url": podcast.url,
            "description": textwrap.shorten(podcast.description, width=500, placeholder="..."),
        }
        for podcast in podcasts[:20]
    ]
    return {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "instructions": (
            "You rank Spotify podcast shows for a daily history notification. "
            "Choose only shows that are clearly relevant to the specific historical event. "
            "Prefer exact event-title matches, then strongly related era/topic matches. "
            "Reject shows about a different conflict, person, place, era, or broad generic history."
        ),
        "input": json.dumps(
            {
                "event": {
                    "year": event.year,
                    "title": event.title,
                    "text": event.text,
                    "wikipedia_url": event.wikipedia_url,
                },
                "spotify_candidates": candidates,
            }
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "podcast_matches",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "urls": {
                            "type": "array",
                            "description": "Spotify URLs for the best matching shows, best first.",
                            "items": {"type": "string"},
                            "maxItems": 5,
                        }
                    },
                    "required": ["urls"],
                },
            }
        },
    }


def parse_openai_selected_urls(payload: dict) -> list[str]:
    text = payload.get("output_text") or response_output_text(payload)
    parsed = json.loads(text)
    urls = parsed.get("urls", [])
    if not isinstance(urls, list):
        return []
    return [url for url in urls if isinstance(url, str)]


def response_output_text(payload: dict) -> str:
    parts = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                parts.append(str(content.get("text", "")))
    return "".join(parts)


def openai_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}",
        "Content-Type": "application/json",
        "User-Agent": "daily-history-notifier/1.0",
    }


def debug_enabled() -> bool:
    return os.getenv("DEBUG_MATCHING", "").lower() in {"1", "true", "yes"}


def debug_matching(message: str) -> None:
    if debug_enabled():
        print(f"[debug] {message}")


def debug_podcast_list(label: str, podcasts: list[Podcast]) -> None:
    if not debug_enabled():
        return
    print(f"[debug] {label}: {len(podcasts)}")
    for index, podcast in enumerate(podcasts, start=1):
        print(f"[debug] {index}. {podcast.name} | {podcast.url}")


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


def podcast_matches_event(podcast: Podcast, event: HistoricalEvent) -> bool:
    return score_podcast_match(podcast, event) >= 2


def score_podcast_match(podcast: Podcast, event: HistoricalEvent) -> int:
    event_context = f"{event.title} {event.text}"
    podcast_name = normalize_search_text(podcast.name)
    podcast_context = normalize_search_text(f"{podcast.name} {podcast.description}")
    score = 0

    title_phrase = normalize_search_text(event.title)
    if len(podcast_relevance_terms(title_phrase)) >= 1 and contains_phrase(podcast_name, title_phrase):
        score += 5
    elif len(podcast_relevance_terms(title_phrase)) >= 1 and contains_phrase(podcast_context, title_phrase):
        score += 3

    for phrase in event_relevance_phrases(event_context):
        if contains_phrase(podcast_context, phrase):
            score += 2

    event_terms = podcast_relevance_terms(event_context)
    podcast_terms = podcast_relevance_terms(podcast_context)
    score += len(event_terms & podcast_terms)
    return score


def event_relevance_phrases(value: str) -> set[str]:
    normalized = normalize_search_text(value)
    phrases = set(re.findall(r"\bworld war (?:i|ii)\b", normalized))
    for chunk in re.split(r"[:.;,]", normalized):
        for match in re.findall(r"\bbattle of (?:the )?[a-z0-9]+(?: [a-z0-9]+){0,3}", chunk):
            phrase = re.split(r"\bworld war\b", match, maxsplit=1)[0].strip()
            if len(podcast_relevance_terms(phrase)) >= 1:
                phrases.add(phrase)
    return {phrase.strip() for phrase in phrases}


def contains_phrase(value: str, phrase: str) -> bool:
    return bool(re.search(rf"\b{re.escape(phrase)}\b", value))


def podcast_relevance_terms(value: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9]+", normalize_search_text(value))
        if len(term) >= 4 and term not in TITLE_STOP_WORDS
    }


def normalize_search_text(value: str) -> str:
    normalized = value.lower()
    replacements = {
        "wwi": "world war i",
        "ww1": "world war i",
        "world war 1": "world war i",
        "first world war": "world war i",
        "wwii": "world war ii",
        "ww2": "world war ii",
        "world war 2": "world war ii",
        "second world war": "world war ii",
    }
    for old, new in replacements.items():
        normalized = re.sub(rf"\b{re.escape(old)}\b", new, normalized)
    return normalized


def render_notification(
    event: HistoricalEvent,
    podcasts: list[Podcast],
    month: int,
    day: int,
    podcast_search_attempted: bool,
) -> str:
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
        spotify_message = (
            "No relevant Spotify podcast matches were found."
            if podcast_search_attempted
            else "Spotify podcast matches were skipped because Spotify credentials are not configured."
        )
        lines.extend(
            [
                "",
                spotify_message,
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
            headers={
                "Content-Type": "application/json",
                "User-Agent": "daily-history-notifier/1.0",
            },
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
