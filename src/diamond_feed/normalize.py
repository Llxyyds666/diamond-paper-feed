from dataclasses import replace
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


def identity_aliases(record: PaperRecord) -> set[str]:
    """Strong identifiers only: never equate unrelated works by similar titles."""
    aliases = {record_key(record)}
    doi = normalize_doi(record.doi) or ""
    arxiv = re.fullmatch(r"10\.48550/arxiv\.(.+?)(?:v\d+)?", doi)
    if arxiv:
        aliases.add("arxiv:" + arxiv[1])
    url = urlparse(record.url)
    if url.hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        match = re.fullmatch(r"/(?:abs|pdf)/(.+?)(?:v\d+)?(?:\.pdf)?/?", url.path)
        if match:
            aliases.add("arxiv:" + match[1].lower())
    figshare = re.fullmatch(r"10\.6084/m9\.figshare\.(\d+)(?:\.v\d+)?", doi)
    if figshare:
        aliases.add("figshare:" + figshare[1])
    return aliases


def group_records(records: list[PaperRecord]) -> dict[str, tuple[PaperRecord, list[str]]]:
    """Merge metadata by stable identifiers; retain every original key for decisions."""
    parents = list(range(len(records)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    seen = {}
    for index, record in enumerate(records):
        for alias in identity_aliases(record):
            if alias in seen:
                parents[root(index)] = root(seen[alias])
            seen[alias] = index
    groups = {}
    for index, record in enumerate(records):
        groups.setdefault(root(index), []).append(record)
    result = {}
    for members in groups.values():
        # Prefer a DOI-bearing representative, then the unversioned identifier.
        ordered = sorted(members, key=lambda p: (not bool(p.doi), len(record_key(p)), record_key(p)))
        merged = ordered[0]
        for other in ordered[1:]:
            merged = merge_records(merged, other)
        if not merged.doi:
            # Keep an existing title/year key even when version metadata changed.
            merged = replace(merged, title=ordered[0].title, published_at=ordered[0].published_at)
        result[record_key(merged)] = (merged, [record_key(p) for p in members])
    return result


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
