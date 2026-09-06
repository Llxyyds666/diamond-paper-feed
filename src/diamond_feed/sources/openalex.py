"""OpenAlex work-record adapter."""

from datetime import date, datetime, timezone
import json
from urllib.parse import urlencode

from diamond_feed.models import PaperRecord
from diamond_feed.normalize import normalize_doi


_BASE_URL = "https://api.openalex.org/works"
_SELECT = "id,doi,title,display_name,publication_date,authorships,primary_location,abstract_inverted_index"


def build_url(query: str, from_date: date, rows: int) -> str:
    """Build the OpenAlex works request for a caller-supplied date window."""
    return f"{_BASE_URL}?{urlencode({'search': query, 'filter': f'from_publication_date:{from_date.isoformat()}', 'per-page': rows, 'select': _SELECT})}"


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str:
    """Recreate an OpenAlex inverted-index abstract in position order."""
    if not index:
        return ""
    positions = [(position, word) for word, offsets in index.items() for position in offsets]
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


def parse_response(body: bytes) -> list[PaperRecord]:
    """Parse OpenAlex JSON, ignoring individual incomplete records."""
    payload = json.loads(body)
    items = payload.get("results", []) if isinstance(payload, dict) else []
    records: list[PaperRecord] = []
    for item in items if isinstance(items, list) else []:
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
