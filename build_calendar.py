"""Build a subscribable iCalendar feed from Agenda Eletrônica RJ.

The script runs in GitHub Actions and fails rather than publishing an empty feed
if the source page changes unexpectedly.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

SOURCE_URL = "https://agendaeletronica.com.br/rj/"
SECONDARY_SOURCE_URL = "https://melhoresfestasdorio.com.br/eletronico/"
OUTPUT_PATH = Path(__file__).resolve().parent / "agenda-eletronica-rj.ics"
TZ = ZoneInfo("America/Sao_Paulo")
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}
SECONDARY_MONTHS = {
    **MONTHS,
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
SECONDARY_DATE_TIME = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>[a-zçãéêô]+)\.?\s+"
    r"(?P<year>\d{4})\s+às\s+(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2})|h(?P<minute_h>\d{2})?)",
    re.IGNORECASE,
)
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
KNOWN_VENUES = [
    "Museu do Amanhã", "Silk Beach Club", "Privilège Búzios",
    "D-EDGE Rio", "Trauma", "Crema Club", "Privilège",
]


@dataclass(frozen=True)
class Event:
    title: str
    event_date: date
    hour: int
    minute: int
    location: str
    ticket_url: str
    source_url: str = SOURCE_URL


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_secondary_date_time(text: str) -> tuple[date, int, int] | None:
    match = SECONDARY_DATE_TIME.search(clean_text(text))
    if not match:
        return None
    month = SECONDARY_MONTHS.get(match.group("month").lower().rstrip("."))
    if not month:
        return None
    try:
        event_date = date(int(match.group("year")), month, int(match.group("day")))
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or match.group("minute_h") or 0)
    except ValueError:
        return None
    if hour > 23 or minute > 59:
        return None
    return event_date, hour, minute


def normalized_event_title(value: str) -> str:
    """Normalize title variants across sources without depending on their URLs."""
    title = clean_text(value)
    title = re.split(r"\s+(?:\||—|–|-)\s+", title, maxsplit=1)[0]
    title = re.sub(
        r"\s+(?:na|no|em|ao|à|as)\s+"
        r"(?:d[\s-]?edge|qualistage|copacabana palace|museu do amanhã|"
        r"privilege|privil[eè]ge|silk beach club|trauma)(?:\b.*)?$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_title.lower()).strip()


def same_event(left: Event, right: Event) -> bool:
    """Return True when two sources are describing the same event."""
    if left.event_date != right.event_date:
        return False
    left_title = normalized_event_title(left.title)
    right_title = normalized_event_title(right.title)
    if not left_title or not right_title:
        return False
    return left_title == right_title or SequenceMatcher(None, left_title, right_title).ratio() >= 0.92


RJ_LOCATION_HINTS = (
    "rio de janeiro", "buzios", "niteroi", "copacabana", "barra da tijuca",
    "joa", "urca", "d-edge", "qualistage", "jardins do mam",
    "museu de arte moderna", "museu do amanha", "leblon", "jockey club",
    "edificio touring", "sheraton", "sambodromo", "apoteose", "privilege",
    "praia rasa", "fishbone", "centro do rio", "arraial do cabo", "cabo frio",
    "angra dos reis", "saquarema", "petropolis", "teresopolis",
)
NON_RJ_HINTS = (
    "sao paulo", "cotia", "cumbuco", "ceara", "pernambuco", "maracaipe",
    "aerodromo nascimento",
)


def is_rio_event(location: str, title: str = "") -> bool:
    location_ascii = unicodedata.normalize("NFKD", location).encode("ascii", "ignore").decode("ascii").lower()
    all_text = unicodedata.normalize("NFKD", f"{location} {title}").encode("ascii", "ignore").decode("ascii").lower()
    if any(hint in all_text for hint in NON_RJ_HINTS):
        return False
    return any(hint in location_ascii for hint in RJ_LOCATION_HINTS)


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
        or bool(re.search(r"\b\d{1,2}\s+de\s+[a-zçãéêô]+\s+às\s+\d{1,2}h", value, re.IGNORECASE))
        or bool(re.search(r"(?:RJ|Rio de Janeiro)\s*$", value, re.IGNORECASE))
    )


def split_repeated_venue(core: str) -> tuple[str, str]:
    """Split duplicated venue labels or a recognized venue at the end of a listing."""
    words = core.split()
    # Listings often repeat the venue after the event title. Prefer that exact pattern.
    for width in range(min(len(words) // 2, 6), 0, -1):
        suffix = " ".join(words[-width:])
        prefix = " ".join(words[:-width]).rstrip(" -–—|:")
        if prefix and prefix.casefold().endswith(suffix.casefold()):
            title = prefix.rstrip(" -–—|:")
            if title:
                return title, suffix
    # Some entries append the venue only once, without a separator.
    for venue in sorted(KNOWN_VENUES, key=len, reverse=True):
        if core.casefold().endswith((" " + venue).casefold()):
            title = core[:-len(venue)].rstrip(" -–—|:")
            if title:
                return title, venue
    return core.strip(), ""


def parse_anchor(anchor: Tag, event_date: date) -> Event | None:
    href = anchor.get("href", "").strip()
    if not href:
        return None
    absolute_url = urljoin(SOURCE_URL, href)
    host = (urlparse(absolute_url).hostname or "").lower()
    if not host or host.endswith(("agendaeletronica.com.br", "facebook.com")):
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

    core_with_city = DATETIME_SUFFIX.sub("", visible)
    core_with_city = TIME_SUFFIX.sub("", core_with_city).strip()
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
        url_match = re.search(r"^URL:(.+)$", block, re.MULTILINE)
        title_match = re.search(r"^SUMMARY:(.+)$", block, re.MULTILINE)
        location_match = re.search(r"^LOCATION:(.+)$", block, re.MULTILINE)
        if url_match and title_match and location_match:
            url = url_match.group(1).strip().rstrip("/")
            title = unescape_ics_text(title_match.group(1).strip())
            location = unescape_ics_text(location_match.group(1).strip())
            context[url] = (title, location)
    return context


def scrape_agenda_eletronica(
    reference_time: datetime,
    existing_context: dict[str, tuple[str, str]],
) -> list[Event]:
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
            elif re.search(r"festas eletrônicas por estado|festas eletr[oô]nicas por estado", heading_text, re.IGNORECASE):
                active_date = None
            continue
        if active_date is None or not isinstance(node, Tag):
            continue
        event = parse_anchor(node, active_date)
        if event and datetime(
            event.event_date.year, event.event_date.month, event.event_date.day,
            event.hour, event.minute, tzinfo=TZ,
        ) >= reference_time:
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
    return sorted(unique.values(), key=lambda e: (e.event_date, e.hour, e.minute, e.title.casefold()))


def scrape_melhores_festas(reference_time: datetime) -> list[Event]:
    response = requests.get(
        SECONDARY_SOURCE_URL,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AgendaEletronicaCalendar/1.0; +https://github.com/)"
        },
    )
    response.raise_for_status()
    if len(response.text) < 1000:
        raise RuntimeError("A segunda fonte retornou conteúdo inesperadamente curto.")

    soup = BeautifulSoup(response.text, "html.parser")
    events: list[Event] = []
    headings = soup.find_all(["h2", "h3", "h4", "h5", "h6"])
    for heading in headings:
        title = clean_text(heading.get_text(" ", strip=True))
        if not title or title.casefold() in {"eletrônico", "eletronico", "melhores festas do rio"}:
            continue

        card: Tag | None = None
        date_time: tuple[date, int, int] | None = None
        parts: list[str] = []
        for depth, ancestor in enumerate(heading.parents):
            if depth > 8 or not isinstance(ancestor, Tag) or ancestor.name in {"body", "html"}:
                break
            text = clean_text(ancestor.get_text(" ", strip=True))
            parsed = parse_secondary_date_time(text)
            if not parsed or title.casefold() not in text.casefold():
                continue
            nested_headings = ancestor.find_all(["h2", "h3", "h4", "h5", "h6"])
            if len(nested_headings) != 1:
                continue
            candidate_parts = list(dict.fromkeys(
                clean_text(part) for part in ancestor.stripped_strings if clean_text(part)
            ))
            if len(candidate_parts) >= 2:
                card, date_time, parts = ancestor, parsed, candidate_parts
                if len(candidate_parts) >= 3:
                    break

        if card is None or date_time is None:
            continue
        event_date, hour, minute = date_time
        if datetime(event_date.year, event_date.month, event_date.day, hour, minute, tzinfo=TZ) < reference_time:
            continue

        location = ""
        for child in card.find_all(True):
            classes = child.get("class", [])
            class_text = " ".join(classes) if isinstance(classes, list) else str(classes)
            attrs = " ".join([class_text, str(child.get("itemprop", "")), str(child.get("aria-label", ""))])
            if re.search(r"location|venue|local|place|address|endereco", attrs, re.IGNORECASE):
                candidate = clean_text(child.get_text(" ", strip=True))
                if candidate and not parse_secondary_date_time(candidate) and title.casefold() not in candidate.casefold():
                    location = candidate
                    break

        if not location:
            for part in parts:
                if part.casefold() == title.casefold() or parse_secondary_date_time(part):
                    continue
                if re.search(r"saiba mais|ver evento|ingressos|comprar|leia mais|compartilhar", part, re.IGNORECASE):
                    continue
                if is_rio_event(part, title):
                    location = part
                    break

        if not location:
            full_text = clean_text(card.get_text(" ", strip=True))
            date_match = SECONDARY_DATE_TIME.search(full_text)
            remainder = full_text
            if date_match:
                remainder = remainder.replace(date_match.group(0), "", 1)
            remainder = re.sub(re.escape(title), "", remainder, count=1, flags=re.IGNORECASE).strip(" -|–—,")
            if is_rio_event(remainder, title):
                location = remainder

        if not location or not is_rio_event(location, title):
            continue

        event_url = ""
        links = card.find_all("a", href=True)
        social_domains = ("facebook.com", "instagram.com", "whatsapp.com", "youtube.com", "tiktok.com")
        for link in links:
            candidate_url = urljoin(SECONDARY_SOURCE_URL, str(link.get("href", "")).strip())
            host = (urlparse(candidate_url).hostname or "").lower()
            if (
                host
                and "melhoresfestasdorio.com.br" not in host
                and not any(domain in host for domain in social_domains)
                and not host.endswith("agendaeletronica.com.br")
            ):
                event_url = candidate_url
                break
        if not event_url:
            for link in links:
                candidate_url = urljoin(SECONDARY_SOURCE_URL, str(link.get("href", "")).strip())
                parsed_url = urlparse(candidate_url)
                host = (parsed_url.hostname or "").lower()
                if (
                    host.endswith("melhoresfestasdorio.com.br")
                    and parsed_url.path.rstrip("/") not in {"", "/", "/eletronico", "/tipo-de-evento/rio"}
                ):
                    event_url = candidate_url
                    break
        if not event_url:
            slug = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
            slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-") or "evento"
            event_url = f"{SECONDARY_SOURCE_URL}#evento-{slug}-{event_date:%Y%m%d}-{hour:02d}{minute:02d}"

        events.append(Event(title, event_date, hour, minute, location, event_url, SECONDARY_SOURCE_URL))

    if not events:
        raise RuntimeError(
            "Nenhum evento futuro do RJ foi extraído de Melhores Festas do Rio. "
            "A estrutura da página pode ter mudado; o feed anterior será preservado."
        )
    unique: list[Event] = []
    for event in events:
        if not any(same_event(event, previous) for previous in unique):
            unique.append(event)
    return sorted(unique, key=lambda e: (e.event_date, e.hour, e.minute, e.title.casefold()))


def scrape_events(now: datetime | date | None = None) -> list[Event]:
    if now is None:
        reference_time = datetime.now(TZ)
    elif isinstance(now, datetime):
        reference_time = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    else:
        reference_time = datetime.combine(now, datetime.min.time(), tzinfo=TZ)

    existing_context = read_existing_feed_context()
    primary_events = scrape_agenda_eletronica(reference_time, existing_context)
    secondary_events = scrape_melhores_festas(reference_time)
    combined: list[Event] = []
    for event in [*primary_events, *secondary_events]:
        if not any(same_event(event, previous) for previous in combined):
            combined.append(event)
    result = sorted(combined, key=lambda e: (e.event_date, e.hour, e.minute, e.title.casefold()))
    if not result:
        raise RuntimeError("Nenhum evento futuro foi extraído das fontes; o feed anterior foi preservado.")
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
    stamp = (generated_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
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
        utc_start = local_start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        digest_input = (
            event.ticket_url.rstrip("/") + event.event_date.isoformat()
            + event.title.casefold() + f"{event.hour:02d}{event.minute:02d}"
        )
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:24]
        link_label = "Compra de ingressos" if event.source_url == SOURCE_URL else "Mais informações"
        description = f"{link_label}: {event.ticket_url}\nAgenda de origem: {event.source_url}"
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


def write_feed_atomically(content: str, output_path: Path | None = None) -> None:
    """Replace the public feed atomically so readers never see a partial file."""
    target = output_path or OUTPUT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def normalize_feed_timestamps(content: str) -> str:
    """Compare calendar data while ignoring generation-only timestamps and newline style."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(
        r"^(DTSTAMP|LAST-MODIFIED):\d{8}T\d{6}Z$",
        r"\1:TIMESTAMP",
        normalized,
        flags=re.MULTILINE,
    )

def event_from_vevent(block: str) -> Event | None:
    """Decode fields needed for duplicate detection from one unfolded VEVENT block."""
    unfolded = re.sub(r"\r?\n[ \t]", "", block)

    def property_value(name: str) -> str:
        match = re.search(rf"^{name}[^:]*:(.*)$", unfolded, re.MULTILINE)
        return match.group(1).strip() if match else ""

    start_value = property_value("DTSTART")
    try:
        start_utc = datetime.strptime(start_value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    local_start = start_utc.astimezone(TZ)
    title = unescape_ics_text(property_value("SUMMARY"))
    location = unescape_ics_text(property_value("LOCATION"))
    url = property_value("URL")
    description = unescape_ics_text(property_value("DESCRIPTION"))
    source_match = re.search(r"Agenda de origem:\s*(https?://[^\s]+)", description)
    source_url = source_match.group(1) if source_match else (
        SECONDARY_SOURCE_URL if "melhoresfestasdorio.com.br" in url else SOURCE_URL
    )
    return Event(title, local_start.date(), local_start.hour, local_start.minute, location, url, source_url)


def _vevent_blocks(content: str) -> list[str]:
    return re.findall(r"BEGIN:VEVENT\r?\n.*?END:VEVENT(?:\r?\n)?", content, re.DOTALL)


def merge_feed_preserving_existing(
    existing_content: str,
    fresh_content: str,
    now: datetime | None = None,
) -> str:
    """Keep existing future VEVENT blocks byte-for-byte and append only new events."""
    reference_time = now or datetime.now(TZ)
    reference_time = reference_time.replace(tzinfo=TZ) if reference_time.tzinfo is None else reference_time.astimezone(TZ)
    old_blocks = _vevent_blocks(existing_content)
    new_blocks = _vevent_blocks(fresh_content)
    if "BEGIN:VCALENDAR" not in existing_content or not old_blocks:
        return fresh_content

    header = existing_content.split("BEGIN:VEVENT", 1)[0]
    header = re.sub(r"(?:\r?\n)?END:VCALENDAR\s*$", "", header).rstrip("\r\n")
    retained_blocks: list[str] = []
    known_events: list[Event] = []
    for block in old_blocks:
        event = event_from_vevent(block)
        if not event:
            continue
        start = datetime(event.event_date.year, event.event_date.month, event.event_date.day,
                         event.hour, event.minute, tzinfo=TZ)
        if start >= reference_time:
            retained_blocks.append(block.rstrip("\r\n"))
            known_events.append(event)

    for block in new_blocks:
        event = event_from_vevent(block)
        if not event:
            continue
        start = datetime(event.event_date.year, event.event_date.month, event.event_date.day,
                         event.hour, event.minute, tzinfo=TZ)
        if start < reference_time or any(same_event(event, known) for known in known_events):
            continue
        retained_blocks.append(block.rstrip("\r\n"))
        known_events.append(event)

    event_text = "".join(block + "\r\n" for block in retained_blocks)
    return header + "\r\n" + event_text + "END:VCALENDAR\r\n"


def main() -> int:
    try:
        events = scrape_events()
        fresh_content = build_ics(events)
        content = fresh_content
        if OUTPUT_PATH.exists():
            # Preserve CRLF bytes from existing VEVENT blocks when merging.
            existing = OUTPUT_PATH.read_bytes().decode("utf-8", errors="replace")
            content = merge_feed_preserving_existing(existing, fresh_content)
            if normalize_feed_timestamps(existing) == normalize_feed_timestamps(content):
                print("Os eventos não mudaram; preservando o feed atual sem alterar os timestamps.")
            else:
                write_feed_atomically(content)
                print(f"Feed atualizado: {len(events)} eventos futuros -> {OUTPUT_PATH.name}")
        else:
            write_feed_atomically(content)
            print(f"Feed criado: {len(events)} eventos futuros -> {OUTPUT_PATH.name}")
        for event in events:
            print(f"- {event.event_date.isoformat()} {event.hour:02d}:{event.minute:02d} | {event.title} | {event.location}")
        return 0
    except (requests.RequestException, OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
