#!/usr/bin/env python3
"""Build a subscribable iCalendar feed from Agenda Eletrônica RJ.

The script runs in GitHub Actions and fails rather than publishing an empty feed
if the source page changes unexpectedly.
"""
from __future__ import annotations

import hashlib
import html
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

SOURCE_URL = "https://agendaeletronica.com.br/rj/"
OUTPUT_PATH = Path(__file__).resolve().parent / "agenda-eletronica-rj.ics"
TZ = ZoneInfo("America/Sao_Paulo")
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}
DATE_HEADING = re.compile(
    r"(?:segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)(?:-feira)?\s*,?\s*"
    r"(?P<day>\d{1,2})\s+de\s+(?P<month>[a-zçãéêô]+)\s+de\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
TIME_SUFFIX = re.compile(r"\bàs\s*(?P<hour>\d{1,2})h(?P<minute>\d{2})?\s*$", re.IGNORECASE)
DATETIME_SUFFIX = re.compile(
    r"\s+\d{1,2}\s+de\s+[a-zçãéêô]+\s+às\s+\d{1,2}h\d{0,2}\s*$",
    re.IGNORECASE,
)
CITY_SUFFIX = re.compile(r"\s*\|\s*(?P<city>[^|]+?-RJ)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Event:
    title: str
    event_date: date
    hour: int
    minute: int
    location: str
    ticket_url: str


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_heading_date(text: str) -> date | None:
    match = DATE_HEADING.search(clean_text(text))
    if not match:
        return None
    month = MONTHS.get(match.group("month").lower())
    if not month:
        return None
    try:
        return date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None


def is_time_or_location_piece(value: str) -> bool:
    value = clean_text(value)
    return (
        not value or value in {"|", "—", "–", "-"}
        or bool(TIME_SUFFIX.search(value))
        or bool(re.search(r"\b\d{1,2}\s+de\s+[a-zçãéêô]+\s+às\s+\d{1,2}h", value, re.I))
        or bool(re.search(r"(?:RJ|Rio de Janeiro)\s*$", value, re.I))
    )


def split_repeated_venue(core: str) -> tuple[str, str]:
    """Split repeated trailing venue labels, e.g. 'Evento D-EDGE Rio D-EDGE Rio'."""
    words = core.split()
    for width in range(min(len(words) // 2, 6), 0, -1):
        suffix = " ".join(words[-width:])
        prefix = " ".join(words[:-width]).rstrip(" -–—|:")
        if prefix and prefix.casefold().endswith(suffix.casefold()):
            title = prefix.rstrip(" -–—|:")
            if title:
                return title, suffix
    return core.strip(), ""


def parse_anchor(anchor: Tag, event_date: date) -> Event | None:
    href = anchor.get("href", "").strip()
    if not href:
        return None
    absolute_url = urljoin(SOURCE_URL, href)
    host = (urlparse(absolute_url).hostname or "").lower()
    if not host or host.endswith("agendaeletronica.com.br") or host.endswith("facebook.com"):
        return None

    visible = clean_text(anchor.get_text(" ", strip=True))
    if not visible:
        return None

    time_match = TIME_SUFFIX.search(visible)
    if not time_match:
        alt_text = clean_text(anchor.get("aria-label", "") or anchor.get("title", ""))
        time_match = TIME_SUFFIX.search(alt_text)
        if time_match:
            visible = alt_text
    if not time_match:
        return None
    hour = int(time_match.group("hour"))
    minute = int(time_match.group("minute") or 0)
    if hour > 23 or minute > 59:
        return None

    core_with_city = DATETIME_SUFFIX.sub("", visible).strip()
    city_match = CITY_SUFFIX.search(core_with_city)
    city = clean_text(city_match.group("city")) if city_match else ""
    core = CITY_SUFFIX.sub("", core_with_city).strip() if city_match else core_with_city

    semantic_tags = ["span", "strong", "b", "em"]
    semantic_nodes = [
        node for node in anchor.find_all(semantic_tags)
        if not node.find(semantic_tags)
    ]
    pieces = [clean_text(node.get_text(" ", strip=True)) for node in semantic_nodes]
    pieces = [piece for piece in pieces if piece and piece not in {"|", "—", "–"}]
    pieces = [piece for piece in pieces if not is_time_or_location_piece(piece)]
    title = ""
    venue = ""
    if len(pieces) >= 2:
        first = CITY_SUFFIX.sub("", DATETIME_SUFFIX.sub("", pieces[0])).strip()
        second = CITY_SUFFIX.sub("", DATETIME_SUFFIX.sub("", pieces[1])).strip()
        if first and second and first.casefold() != second.casefold():
            title, venue = first, second

    if not title:
        title, venue = split_repeated_venue(core)

    title = clean_text(title or core)
    venue = clean_text(venue)
    if not title:
        return None

    location = venue
    if city:
        location = f"{venue}, {city}" if venue else city
    if not location:
        location = "Rio de Janeiro-RJ"

    return Event(title, event_date, hour, minute, location, absolute_url)


def unescape_ics_text(value: str) -> str:
    return (value.replace(r"\n", "\n").replace(r"\,", ",")
            .replace(r"\;", ";").replace(r"\\", "\\"))


def read_existing_feed_context() -> dict[str, tuple[str, str]]:
    """Reuse prior title/location for known ticket URLs when HTML loses boundaries."""
    if not OUTPUT_PATH.exists():
        return {}
    raw = OUTPUT_PATH.read_text(encoding="utf-8", errors="replace")
    raw = re.sub(r"\r?\n[ \t]", "", raw)
    context: dict[str, tuple[str, str]] = {}
    for block in raw.split("BEGIN:VEVENT")[1:]:
        url_match = re.search(r"^URL:(.+)$", block, re.M)
        title_match = re.search(r"^SUMMARY:(.+)$", block, re.M)
        location_match = re.search(r"^LOCATION:(.+)$", block, re.M)
        if url_match and title_match and location_match:
            url = url_match.group(1).strip().rstrip("/")
            title = unescape_ics_text(title_match.group(1).strip())
            location = unescape_ics_text(location_match.group(1).strip())
            context[url] = (title, location)
    return context


def scrape_events(now: date | None = None) -> list[Event]:
    today = now or datetime.now(TZ).date()
    existing_context = read_existing_feed_context()
    response = requests.get(
        SOURCE_URL,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AgendaEletronicaCalendar/1.0; +https://github.com/)"
        },
    )
    response.raise_for_status()
    if len(response.text) < 1000:
        raise RuntimeError("A página de origem retornou conteúdo inesperadamente curto.")

    soup = BeautifulSoup(response.text, "html.parser")
    events: list[Event] = []
    active_date: date | None = None
    headings_and_links = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a"])
    for node in headings_and_links:
        if node.name and node.name.startswith("h"):
            heading_text = clean_text(node.get_text(" ", strip=True))
            parsed_date = parse_heading_date(heading_text)
            if parsed_date:
                active_date = parsed_date
            elif re.search(r"festas eletrônicas por estado|festas eletr[oô]nicas por estado", heading_text, re.I):
                active_date = None
            continue
        if active_date is None or not isinstance(node, Tag):
            continue
        event = parse_anchor(node, active_date)
        if event and event.event_date >= today:
            cached = existing_context.get(event.ticket_url.rstrip("/"))
            if cached:
                city_only = "," not in event.location
                partial_title = (
                    event.location == cached[1]
                    and event.title.casefold() != cached[0].casefold()
                    and event.title.casefold() in cached[0].casefold()
                )
                if city_only or partial_title:
                    event = Event(cached[0], event.event_date, event.hour, event.minute, cached[1], event.ticket_url)
            events.append(event)

    unique: dict[tuple[str, date], Event] = {}
    for event in events:
        unique[(event.ticket_url.rstrip("/"), event.event_date)] = event
    result = sorted(unique.values(), key=lambda e: (e.event_date, e.hour, e.minute, e.title.casefold()))
    if not result:
        raise RuntimeError(
            "Nenhum evento futuro foi extraído. O HTML pode ter mudado ou a origem bloqueou a consulta; "
            "o feed existente foi preservado porque a atualização falhou."
        )
    return result


def ics_escape(value: str) -> str:
    raw_lines = html.unescape(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    escaped_lines = []
    for raw_line in raw_lines:
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        line = line.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,")
        escaped_lines.append(line)
    return r"\n".join(escaped_lines)


def fold_ics_line(line: str, limit: int = 75) -> str:
    """Fold content lines at 75 UTF-8 octets as required by RFC 5545."""
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for char in line:
        char_bytes = len(char.encode("utf-8"))
        if current and current_bytes + char_bytes > limit:
            chunks.append(current)
            current = " " + char
            current_bytes = 1 + char_bytes
        else:
            current += char
            current_bytes += char_bytes
    chunks.append(current)
    return "\r\n".join(chunks)


def build_ics(events: list[Event], generated_at: datetime | None = None) -> str:
    stamp = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Agenda Eletronica//Calendario RJ//PT-BR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Agenda Eletrônica RJ — festas e ingressos",
        "X-WR-TIMEZONE:America/Sao_Paulo",
    ]
    for event in events:
        local_start = datetime(
            event.event_date.year, event.event_date.month, event.event_date.day,
            event.hour, event.minute, tzinfo=TZ,
        )
        utc_start = local_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha256((event.ticket_url.rstrip("/") + event.event_date.isoformat()).encode("utf-8")).hexdigest()[:24]
        description = f"Compra de ingressos: {event.ticket_url}\nAgenda de origem: {SOURCE_URL}"
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{digest}@agendaeletronica-rj",
            f"DTSTAMP:{stamp}",
            f"LAST-MODIFIED:{stamp}",
            f"DTSTART:{utc_start}",
            f"SUMMARY:{ics_escape(event.title)}",
            f"LOCATION:{ics_escape(event.location)}",
            f"DESCRIPTION:{ics_escape(description)}",
            f"URL:{event.ticket_url}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def main() -> int:
    try:
        events = scrape_events()
        content = build_ics(events)
        OUTPUT_PATH.write_text(content, encoding="utf-8", newline="")
        print(f"Feed atualizado: {len(events)} eventos futuros -> {OUTPUT_PATH.name}")
        for event in events:
            print(f"- {event.event_date.isoformat()} {event.hour:02d}:{event.minute:02d} | {event.title} | {event.location}")
        return 0
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
