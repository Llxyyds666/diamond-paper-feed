import re
import unicodedata
from urllib.parse import urlparse

from diamond_feed.models import PaperRecord


DOI_PREFIXES = ("https://doi.org/", "http://dx.doi.org/", "doi:")
_GENERIC_URL_HOSTS = {"example.test", "api.crossref.org", "crossref.org", "arxiv.org"}


def normalize_doi(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    for prefix in DOI_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized or None


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE).strip()


def record_key(record: PaperRecord) -> str:
    doi = normalize_doi(record.doi)
    if doi:
        return f"doi:{doi}"
    return f"title:{_normalized_title(record.title)}:{record.published_at.year}"


def _stable_union(first: list[str], second: list[str]) -> list[str]:
    result: list[str] = []
    for value in [*first, *second]:
        if value not in result:
            result.append(value)
    return result


def _preferred_text(left: str, right: str) -> str:
    return right if len(right.strip()) > len(left.strip()) else left


def _url_quality(value: str) -> int:
    host = urlparse(value).netloc.casefold()
    if host == "doi.org" or host.endswith(".doi.org"):
        return 2
    if host and host not in _GENERIC_URL_HOSTS and not host.startswith(("api.", "rss.", "feed.")):
        return 1
    return 0


def _preferred_url(left: str, right: str) -> str:
    return right if _url_quality(right) > _url_quality(left) else left


def merge_records(left: PaperRecord, right: PaperRecord) -> PaperRecord:
    """Return a new, deterministic merge without modifying either input record."""
    return PaperRecord(
        title=_preferred_text(left.title, right.title),
        abstract=_preferred_text(left.abstract, right.abstract),
        authors=list(right.authors) if len(right.authors) > len(left.authors) else list(left.authors),
        journal=_preferred_text(left.journal, right.journal),
        published_at=left.published_at,
        doi=normalize_doi(left.doi) or normalize_doi(right.doi),
        url=_preferred_url(left.url, right.url),
        sources=_stable_union(left.sources, right.sources),
        source_ids=_stable_union(left.source_ids, right.source_ids),
        categories=_stable_union(left.categories, right.categories),
        summary_zh=_preferred_text(left.summary_zh or "", right.summary_zh or "") or None,
        ai_relevant=left.ai_relevant if left.ai_relevant is not None else right.ai_relevant,
        ai_confidence=max(
            (value for value in (left.ai_confidence, right.ai_confidence) if value is not None),
            default=None,
        ),
    )
