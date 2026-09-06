"""arXiv Atom-record adapter."""

from datetime import date, datetime, timezone
from urllib.parse import urlencode
from xml.etree import ElementTree

from diamond_feed.models import PaperRecord


_BASE_URL = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"


def build_url(query: str, from_date: date, rows: int) -> str:
    """Build an arXiv submitted-date query without exposing raw query text."""
    quoted = query.replace('"', r'\"')
    start = from_date.strftime("%Y%m%d") + "0000"
    search_query = f'(all:"{quoted}") AND submittedDate:[{start} TO 300001010000]'
    return f"{_BASE_URL}?{urlencode({'search_query': search_query, 'start': 0, 'max_results': rows, 'sortBy': 'submittedDate', 'sortOrder': 'descending'})}"


def _text(element: ElementTree.Element, name: str) -> str:
    return " ".join((element.findtext(_ATOM + name) or "").split())


def _canonical_id(raw_id: str) -> str:
    value = raw_id.rstrip("/").rsplit("/", 1)[-1]
    return value.split("v", 1)[0]


def _published_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def parse_response(body: bytes) -> list[PaperRecord]:
    """Parse arXiv Atom XML, ignoring malformed individual entries."""
    root = ElementTree.fromstring(body)
    records: list[PaperRecord] = []
    for entry in root.findall(_ATOM + "entry"):
        try:
            raw_id = _text(entry, "id")
            title = _text(entry, "title")
            published = _text(entry, "published")
            if not raw_id or not title or not published:
                continue
            identifier = _canonical_id(raw_id)
            authors = [_text(author, "name") for author in entry.findall(_ATOM + "author")]
            journal = (entry.findtext(_ARXIV + "journal_ref") or "").strip()
            records.append(
                PaperRecord(
                    title=title,
                    abstract=_text(entry, "summary"),
                    authors=[author for author in authors if author],
                    journal=journal,
                    published_at=_published_at(published),
                    doi=None,
                    url=f"https://arxiv.org/abs/{identifier}",
                    sources=["arxiv"],
                    source_ids=[f"arxiv:{identifier}"],
                )
            )
        except (TypeError, ValueError, OverflowError):
            continue
    return records
