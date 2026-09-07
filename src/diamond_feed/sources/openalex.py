"""OpenAlex work-record adapter."""

from datetime import date, datetime, timezone
import json
from urllib.parse import urlencode

from diamond_feed.models import PaperRecord, ScholarlyPage
from diamond_feed.normalize import normalize_doi


_BASE_URL = "https://api.openalex.org/works"
_SELECT = "id,doi,title,display_name,publication_date,authorships,primary_location,abstract_inverted_index"
_MAX_PAGE_SIZE = 200


def build_url(query: str, from_date: date, rows: int, cursor: str = "*") -> str:
    """Build the OpenAlex works request for a caller-supplied date window."""
    page_size = min(rows, _MAX_PAGE_SIZE)
    return f"{_BASE_URL}?{urlencode({'search': query, 'filter': f'from_publication_date:{from_date.isoformat()}', 'per-page': page_size, 'cursor': cursor, 'select': _SELECT})}"


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str:
    """Recreate an OpenAlex inverted-index abstract in position order."""
    if index is None:
        return ""
    if not isinstance(index, dict):
        raise ValueError("abstract inverted index must be an object")
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        if not isinstance(word, str) or not isinstance(offsets, list):
            raise ValueError("abstract inverted index has invalid entries")
        for position in offsets:
            if type(position) is not int or position < 0:
                raise ValueError("abstract inverted index has invalid offsets")
            positions.append((position, word))
    return " ".join(word for _, word in sorted(positions))


def _published_at(value: object) -> datetime:
    return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)


def _authors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for authorship in value:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        if isinstance(author, dict) and author.get("display_name"):
            result.append(str(author["display_name"]))
    return result


def _parse_payload(body: bytes) -> tuple[dict[str, object], list[object]]:
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("invalid OpenAlex response envelope")
    return payload, payload["results"]


def parse_response(body: bytes) -> list[PaperRecord]:
    """Parse OpenAlex JSON, ignoring individual incomplete records."""
    _, items = _parse_payload(body)
    records: list[PaperRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            title = str(item.get("title") or item.get("display_name") or "").strip()
            publication_date = item.get("publication_date")
            source_id = str(item.get("id") or "").strip()
            if not title or not publication_date or not source_id:
                continue
            location = item.get("primary_location")
            location = location if isinstance(location, dict) else {}
            source = location.get("source")
            source = source if isinstance(source, dict) else {}
            records.append(
                PaperRecord(
                    title=title,
                    abstract=reconstruct_abstract(item.get("abstract_inverted_index")),
                    authors=_authors(item.get("authorships")),
                    journal=str(source.get("display_name") or ""),
                    published_at=_published_at(publication_date),
                    doi=normalize_doi(item.get("doi") if isinstance(item.get("doi"), str) else None),
                    url=str(location.get("landing_page_url") or source_id),
                    sources=["openalex"],
                    source_ids=[source_id],
                )
            )
        except (TypeError, ValueError):
            continue
    return records


def parse_page(body: bytes) -> ScholarlyPage:
    """Parse records plus the opaque cursor needed for the next OpenAlex page."""
    payload, items = _parse_payload(body)
    meta = payload.get("meta")
    if not isinstance(meta, dict) or "next_cursor" not in meta:
        raise ValueError("invalid OpenAlex pagination metadata")
    next_cursor = meta["next_cursor"]
    if next_cursor is not None and (type(next_cursor) is not str or not next_cursor):
        raise ValueError("invalid OpenAlex next cursor")
    return ScholarlyPage(parse_response(body), next_cursor, len(items))
