"""Crossref work-record adapter."""

from datetime import date, datetime, timezone
from html import unescape
import json
import re
from urllib.parse import urlencode

from diamond_feed.models import PaperRecord
from diamond_feed.normalize import normalize_doi


_BASE_URL = "https://api.crossref.org/works"
_SELECT = "DOI,title,abstract,author,container-title,published-online,published-print,URL"
_TAGS = re.compile(r"<[^>]+>")


def build_url(query: str, from_date: date, rows: int) -> str:
    """Build the Crossref works request for a caller-supplied date window."""
    return f"{_BASE_URL}?{urlencode({'query.bibliographic': query, 'filter': f'from-pub-date:{from_date.isoformat()}', 'rows': rows, 'select': _SELECT})}"


def _strip_tags(value: object) -> str:
    return " ".join(_TAGS.sub(" ", unescape(str(value or ""))).split())


def _publication_date(item: dict[str, object]) -> datetime:
    """Return UTC publication time, defaulting a missing month or day to one."""
    for field in ("published-online", "published-print"):
        value = item.get(field)
        if not isinstance(value, dict):
            continue
        parts = value.get("date-parts")
        if not (isinstance(parts, list) and parts and isinstance(parts[0], list)):
            continue
        date_parts = parts[0]
        if not 1 <= len(date_parts) <= 3:
            continue
        try:
            year = int(date_parts[0])
            month = int(date_parts[1]) if len(date_parts) >= 2 else 1
            day = int(date_parts[2]) if len(date_parts) >= 3 else 1
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
    raise ValueError("missing publication date")


def _authors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for author in value:
        if not isinstance(author, dict):
            continue
        name = str(author.get("name") or "").strip()
        if not name:
            name = " ".join(str(author.get(part) or "").strip() for part in ("given", "family")).strip()
        if name:
            result.append(name)
    return result


def parse_response(body: bytes) -> list[PaperRecord]:
    """Parse Crossref JSON, ignoring individual incomplete records."""
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
        raise ValueError("invalid Crossref response envelope")
    message = payload["message"]
    if not isinstance(message.get("items"), list):
        raise ValueError("invalid Crossref response envelope")
    items = message["items"]
    records: list[PaperRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            titles = item.get("title")
            title = str(titles[0]).strip() if isinstance(titles, list) and titles else ""
            doi = normalize_doi(item.get("DOI") if isinstance(item.get("DOI"), str) else None)
            if not title or not doi:
                continue
            journals = item.get("container-title")
            journal = str(journals[0]) if isinstance(journals, list) and journals else ""
            records.append(
                PaperRecord(
                    title=title,
                    abstract=_strip_tags(item.get("abstract")),
                    authors=_authors(item.get("author")),
                    journal=journal,
                    published_at=_publication_date(item),
                    doi=doi,
                    url=str(item.get("URL") or f"https://doi.org/{doi}"),
                    sources=["crossref"],
                    source_ids=[doi],
                )
            )
        except (TypeError, ValueError, OverflowError):
            continue
    return records
