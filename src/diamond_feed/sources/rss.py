from datetime import datetime, timezone
import re
from typing import Callable

import feedparser

from diamond_feed.http import FetchError, HttpResult
from diamond_feed.models import PaperRecord, SourceFailure
from diamond_feed.normalize import normalize_doi


_DOI_PATTERN = re.compile(r"(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[-._;()/:a-z0-9]+)", re.I)
_HEADER_PATTERN = re.compile(r"\s*headers?\s*=\s*[^\n]+", re.I)


def classify_failure(status: int | None, error: Exception | None, empty: bool) -> str:
    if status == 404:
        return "http_404"
    if status == 410:
        return "http_410"
    if isinstance(error, TimeoutError):
        return "timeout"
    if error is not None:
        return "parse_error"
    if empty:
        return "empty_feed"
    return f"http_{status}" if status is not None and status >= 400 else "unknown"


def _extract_doi(entry: object) -> str | None:
    values: list[str] = []
    for field in ("prism_doi", "dc_identifier", "identifier", "doi"):
        value = entry.get(field)  # type: ignore[union-attr]
        if value:
            values.append(str(value))
    for link in entry.get("links", []):  # type: ignore[union-attr]
        href = link.get("href")
        if href:
            values.append(str(href))
    for value in values:
        match = _DOI_PATTERN.search(value)
        if match:
            return normalize_doi(match.group(1))
        normalized = normalize_doi(value)
        if normalized and normalized.startswith("10."):
            return normalized
    return None


def _publication_date(entry: object) -> tuple[datetime, list[str]]:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)  # type: ignore[union-attr]
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc), []
    return datetime.now(timezone.utc), ["missing_date"]


def _entry_url(entry: object, source_url: str) -> str:
    for link in entry.get("links", []):  # type: ignore[union-attr]
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return str(link["href"])
    identifier = str(entry.get("id", ""))  # type: ignore[union-attr]
    return identifier if identifier.startswith(("http://", "https://")) else source_url


def parse_feed(body: bytes, source_url: str) -> list[PaperRecord]:
    parsed = feedparser.parse(body)
    entries = list(parsed.entries)
    if parsed.bozo and not entries:
        raise ValueError("malformed feed")
    journal = str(parsed.feed.get("title", ""))
    records: list[PaperRecord] = []
    for entry in entries:
        published_at, categories = _publication_date(entry)
        authors = [str(author.get("name", "")) for author in entry.get("authors", []) if author.get("name")]
        record_journal = str(entry.get("journal") or entry.get("prism_publicationname") or journal)
        source_id = str(entry.get("id") or entry.get("link") or entry.get("title", ""))
        records.append(
            PaperRecord(
                title=str(entry.get("title", "")),
                abstract=str(entry.get("summary") or entry.get("description") or ""),
                authors=authors,
                journal=record_journal,
                published_at=published_at,
                doi=_extract_doi(entry),
                url=_entry_url(entry, source_url),
                sources=["rss"],
                source_ids=[source_id],
                categories=categories,
            )
        )
    return records


def _safe_detail(detail: str) -> str:
    return _HEADER_PATTERN.sub("", detail)


def collect_rss(
    url: str, fetcher: Callable[[str], HttpResult],
) -> tuple[list[PaperRecord], SourceFailure | None]:
    try:
        result = fetcher(url)
    except FetchError as error:
        return [], SourceFailure(
            timestamp=datetime.now(timezone.utc),
            category=error.category,
            url=url,
            detail=_safe_detail(error.detail),
        )
    except TimeoutError as error:
        return [], SourceFailure(datetime.now(timezone.utc), "timeout", url, str(error))
    except Exception as error:
        return [], SourceFailure(datetime.now(timezone.utc), "network_error", url, _safe_detail(str(error)))

    if not 200 <= result.status < 300:
        return [], SourceFailure(
            timestamp=datetime.now(timezone.utc),
            category=classify_failure(result.status, None, False),
            url=result.final_url or url,
            detail=f"HTTP {result.status}",
        )

    try:
        records = parse_feed(result.body, url)
    except Exception as error:
        return [], SourceFailure(datetime.now(timezone.utc), "parse_error", url, _safe_detail(str(error)))
    if not records:
        return [], SourceFailure(datetime.now(timezone.utc), "empty_feed", url, "feed contained no entries")
    return records, None
