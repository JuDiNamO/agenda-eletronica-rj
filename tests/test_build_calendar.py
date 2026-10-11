from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from icalendar import Calendar

import build_calendar as bc


def test_parse_portuguese_date_heading():
    assert bc.parse_heading_date("domingo, 11 de outubro de 2026") == date(2026, 10, 11)
    assert bc.parse_heading_date("sexta-feira, 16 de outubro de 2026") == date(2026, 10, 16)
    assert bc.parse_heading_date("terça-feira, 31 de fevereiro de 2026") is None
    assert bc.parse_heading_date("Próximas festas") is None


def test_parse_anchor_reads_title_venue_time_and_ticket_url():
    soup = BeautifulSoup(
        '<a href="https://www.ingresse.com/uma-festa/">'
        '<span>Festa de Teste</span><span>Casa XYZ</span> | Rio de Janeiro-RJ <span>às 23h30</span>'
        '</a>',
        "html.parser",
    )
    event = bc.parse_anchor(soup.a, date(2026, 10, 16))
    assert event is not None
    assert event.title == "Festa de Teste"
    assert event.location == "Casa XYZ, Rio de Janeiro-RJ"
    assert (event.hour, event.minute) == (23, 30)
    assert event.ticket_url == "https://www.ingresse.com/uma-festa/"


def test_parse_anchor_splits_known_venue_from_plain_text():
    soup = BeautifulSoup(
        '<a href="https://www.ingresse.com/festa/">Festa eletrônica D-EDGE Rio | Rio de Janeiro-RJ às 23h</a>',
        "html.parser",
    )
    event = bc.parse_anchor(soup.a, date(2026, 10, 16))
    assert event is not None
    assert event.title == "Festa eletrônica"
    assert event.location == "D-EDGE Rio, Rio de Janeiro-RJ"


@pytest.mark.parametrize("hour_text", ["às 24h", "às 25h", "às 23h61"])
def test_parse_anchor_rejects_invalid_times(hour_text):
    soup = BeautifulSoup(
        f'<a href="https://www.ingresse.com/festa/">Festa Teste Casa XYZ {hour_text}</a>',
        "html.parser",
    )
    assert bc.parse_anchor(soup.a, date(2026, 10, 16)) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://agendaeletronica.com.br/rj/",
        "https://www.agendaeletronica.com.br/rj/",
        "https://www.facebook.com/evento",
        "javascript:alert(1)",
        "mailto:teste@example.com",
    ],
)
def test_parse_anchor_ignores_non_ticket_links(url):
    soup = BeautifulSoup(f'<a href="{url}">Festa Teste Casa XYZ às 23h</a>', "html.parser")
    assert bc.parse_anchor(soup.a, date(2026, 10, 16)) is None


def test_ics_escape_handles_special_characters_and_line_breaks():
    escaped = bc.ics_escape("Festa, edição; verão\nnoite")
    assert escaped == r"Festa\, edição\; verão\nnoite"


def test_folded_lines_follow_ical_octet_limit_and_unfold_losslessly():
    source = "SUMMARY:" + ("Festa verão 🎧 " * 12)
    folded = bc.fold_ics_line(source)
    physical_lines = folded.split("\r\n")
    assert len(physical_lines) > 1
    assert all(len(line.encode("utf-8")) <= 75 for line in physical_lines)
    assert all(line.startswith(" ") for line in physical_lines[1:])
    assert "".join(line[1:] if i else line for i, line in enumerate(physical_lines)) == source


def test_build_ics_is_parseable_and_preserves_local_time_and_ticket_link():
    event = bc.Event(
        title="Festa, edição; verão",
        event_date=date(2026, 10, 16),
        hour=23,
        minute=30,
        location="Casa XYZ, Rio de Janeiro-RJ",
        ticket_url="https://www.ingresse.com/festa-teste/",
    )
    timestamp = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    raw = bc.build_ics([event], generated_at=timestamp)
    parsed = Calendar.from_ical(raw)
    events = parsed.walk("VEVENT")
    assert len(events) == 1
    vevent = events[0]
    summary = vevent.decoded("summary")
    assert (summary.decode("utf-8") if isinstance(summary, bytes) else str(summary)) == event.title
    assert vevent.decoded("dtstart") == datetime(2026, 10, 17, 2, 30, tzinfo=UTC)
    location = vevent.decoded("location")
    assert (location.decode("utf-8") if isinstance(location, bytes) else str(location)) == event.location
    assert str(vevent.get("url")) == event.ticket_url
    assert event.ticket_url in str(vevent.decoded("description"))


def test_uid_is_stable_when_only_generation_timestamp_changes():
    event = bc.Event("Festa", date(2026, 10, 16), 23, 0, "Casa", "https://ingresse.com/festa")
    raw_a = bc.build_ics([event], datetime(2026, 10, 11, 10, 0, tzinfo=UTC))
    raw_b = bc.build_ics([event], datetime(2026, 10, 12, 10, 0, tzinfo=UTC))
    cal_a = Calendar.from_ical(raw_a).walk("VEVENT")[0]
    cal_b = Calendar.from_ical(raw_b).walk("VEVENT")[0]
    assert str(cal_a["UID"]) == str(cal_b["UID"])


def test_timestamp_normalizer_ignores_only_timestamps_and_newline_format():
    event = bc.Event("Festa", date(2026, 10, 16), 23, 0, "Casa", "https://ingresse.com/festa")
    raw_a = bc.build_ics([event], datetime(2026, 10, 11, 10, 0, tzinfo=UTC))
    raw_b = bc.build_ics([event], datetime(2026, 10, 12, 10, 0, tzinfo=UTC)).replace("\r\n", "\n")
    assert bc.normalize_feed_timestamps(raw_a) == bc.normalize_feed_timestamps(raw_b)
    changed = raw_b.replace("SUMMARY:Festa", "SUMMARY:Festa nova")
    assert bc.normalize_feed_timestamps(raw_a) != bc.normalize_feed_timestamps(changed)


def test_scraper_excludes_events_that_already_started_today(monkeypatch, tmp_path):
    monkeypatch.setattr(bc, "OUTPUT_PATH", tmp_path / "does-not-exist.ics")
    html = """
      <html><body>
      <h2>domingo, 11 de outubro de 2026</h2>
      <a href="https://www.ingresse.com/passada/"><span>Festa passada</span><span>Casa A</span><span>às 15h</span></a>
      <a href="https://www.ingresse.com/futura/"><span>Festa futura</span><span>Casa B</span><span>às 21h</span></a>
      <div>""" + ("conteúdo extra " * 100) + """</div>
      </body></html>
    """
    response = SimpleNamespace(text=html, raise_for_status=lambda: None)
    monkeypatch.setattr(bc.requests, "get", lambda *args, **kwargs: response)
    now = datetime(2026, 10, 11, 17, 0, tzinfo=bc.TZ)
    events = bc.scrape_events(now=now)
    assert [event.title for event in events] == ["Festa futura"]


def test_checked_in_feed_is_valid_and_has_unique_events():
    feed_path = Path(__file__).resolve().parents[1] / "agenda-eletronica-rj.ics"
    raw = feed_path.read_bytes()
    assert raw.startswith(b"BEGIN:VCALENDAR\r\n")
    assert raw.endswith(b"END:VCALENDAR\r\n")
    assert not re.search(rb"[\t ]+$", raw, re.MULTILINE)
    # RFC 5545 recommends folding at 75 octets, not 75 Unicode characters.
    assert all(len(line) <= 75 for line in raw.split(b"\r\n") if line)
    parsed = Calendar.from_ical(raw)
    events = parsed.walk("VEVENT")
    assert len(events) > 0
    uids = [str(event.get("UID")) for event in events]
    assert len(uids) == len(set(uids))
    for event in events:
        assert not getattr(event, "errors", []), f"Malformed iCalendar properties: {event.errors}"
        assert event.get("UID")
        assert event.get("DTSTAMP")
        assert event.get("SUMMARY")
        assert event.get("DTSTART")
        start = event.decoded("DTSTART")
        assert isinstance(start, datetime)
        assert start.tzinfo is not None
        url = str(event.get("URL"))
        assert url.startswith("https://")
        description = event.decoded("DESCRIPTION")
        if isinstance(description, bytes):
            description = description.decode("utf-8")
        assert url in str(description)
