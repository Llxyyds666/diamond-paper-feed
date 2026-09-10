from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sys
import unicodedata
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from diamond_feed.normalize import normalize_doi
from diamond_feed.state import FeedState


SEMANTIC_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
OPENAIRE_URL = "https://api.openaire.eu/graph/v3/research-products"
MAX_SEMANTIC_RECORDS = 500
MAX_OPENAIRE_RECORDS = 5
MAX_ABSTRACT_CHARS = 30_000
DEFAULT_TIMEOUT_SECONDS = 15.0
_BOILERPLATE = {
    "no abstract available",
    "abstract not available",
    "no abstract",
    "abstract unavailable",
}


@dataclass(frozen=True, slots=True)
class MetadataResponse:
    status: int
    body: bytes


@dataclass(frozen=True, slots=True)
class EnrichmentStats:
    semantic_scholar_queried: int = 0
    openaire_queried: int = 0
    enriched: int = 0


MetadataTransport = Callable[
    [str, str, Mapping[str, str], bytes | None, float], MetadataResponse
]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AbstractEnricher:
    def __init__(
        self,
        semantic_scholar_key: str | None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: MetadataTransport | None = None,
    ) -> None:
        self._semantic_scholar_key = semantic_scholar_key or None
        self._timeout_seconds = timeout_seconds
        self._transport = transport or _default_transport

    def enrich(
        self, state: FeedState, candidate_keys: Sequence[str]
    ) -> EnrichmentStats:
        semantic_keys = _prioritized_missing_keys(state, candidate_keys)[
            :MAX_SEMANTIC_RECORDS
        ]
        semantic_values: dict[str, str] = {}
        if self._semantic_scholar_key and semantic_keys:
            semantic_values = self._semantic_scholar(state, semantic_keys)
        enriched = _apply_values(state, semantic_values, "semantic-scholar")
        openaire_keys = [
            key for key in candidate_keys
            if key in state.papers and _missing(state.papers[key].title, state.papers[key].abstract)
        ]
        openaire_values = self._openaire(state, openaire_keys)
        enriched += _apply_values(state, openaire_values, "openaire")
        return EnrichmentStats(
            semantic_scholar_queried=len(semantic_keys) if self._semantic_scholar_key else 0,
            openaire_queried=len(openaire_keys),
            enriched=enriched,
        )

    def _semantic_scholar(
        self, state: FeedState, keys: Sequence[str]
    ) -> dict[str, str]:
        doi_to_key = _requested_dois(state, keys)
        ids = [f"DOI:{doi}" for doi in doi_to_key]
        if not ids:
            return {}
        url = SEMANTIC_BATCH_URL + "?" + urlencode(
            {"fields": "title,abstract,externalIds"}
        )
        body = json.dumps({"ids": ids}, separators=(",", ":")).encode("utf-8")
        payload = self._request_json(
            "semantic-scholar",
            "POST",
            url,
            {
                "Content-Type": "application/json",
                "x-api-key": self._semantic_scholar_key or "",
            },
            body,
        )
        if type(payload) is not list:
            return {}

        candidates: dict[str, str] = {}
        counts: dict[str, int] = {}
        for result in payload:
            if type(result) is not dict:
                continue
            external_ids = result.get("externalIds")
            if type(external_ids) is not dict or type(external_ids.get("DOI")) is not str:
                continue
            doi = normalize_doi(external_ids["DOI"])
            if doi not in doi_to_key:
                continue
            counts[doi] = counts.get(doi, 0) + 1
            title = result.get("title")
            abstract = result.get("abstract")
            if type(title) is not str or type(abstract) is not str:
                continue
            key = doi_to_key[doi]
            record = state.papers[key]
            cleaned = _valid_abstract(record.title, abstract)
            if _compatible_title(record.title, title) and cleaned is not None:
                candidates[doi] = cleaned
        return {
            doi_to_key[doi]: value
            for doi, value in candidates.items()
            if counts.get(doi) == 1
        }

    def _openaire(
        self, state: FeedState, keys: Sequence[str]
    ) -> dict[str, str]:
        doi_to_key = _requested_dois(state, keys)
        dois = list(doi_to_key)
        candidates: dict[str, str] = {}
        counts: dict[str, int] = {}
        for start in range(0, len(dois), MAX_OPENAIRE_RECORDS):
            batch = dois[start:start + MAX_OPENAIRE_RECORDS]
            pid = " OR ".join(f'"{doi}"' for doi in batch)
            url = OPENAIRE_URL + "?" + urlencode({"pid": pid, "pageSize": 100})
            payload = self._request_json("openaire", "GET", url, {}, None)
            if type(payload) is not dict or type(payload.get("results")) is not list:
                continue
            for result in payload["results"]:
                if type(result) is not dict:
                    continue
                result_dois = _openaire_dois(result.get("pids"))
                for doi in result_dois:
                    if doi not in doi_to_key:
                        continue
                    counts[doi] = counts.get(doi, 0) + 1
                    title = result.get("title")
                    descriptions = result.get("descriptions")
                    if type(title) is not str or type(descriptions) is not list:
                        continue
                    key = doi_to_key[doi]
                    record = state.papers[key]
                    cleaned = next(
                        (
                            valid
                            for value in descriptions
                            if (valid := _valid_abstract(record.title, value)) is not None
                        ),
                        None,
                    )
                    if _compatible_title(record.title, title) and cleaned is not None:
                        candidates[doi] = cleaned
        return {
            doi_to_key[doi]: value
            for doi, value in candidates.items()
            if counts.get(doi) == 1
        }

    def _request_json(
        self,
        provider: str,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> object | None:
        try:
            response = self._transport(
                method, url, headers, body, self._timeout_seconds
            )
            if not 200 <= response.status < 300:
                raise HTTPError(url, response.status, "", {}, None)
            return json.loads(response.body)
        except Exception as error:
            status = error.code if isinstance(error, HTTPError) else None
            _report_provider_error(provider, type(error).__name__, status)
            return None


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> MetadataResponse:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=timeout) as response:
            return MetadataResponse(status=response.status, body=response.read())
    except HTTPError as error:
        return MetadataResponse(status=error.code, body=error.read())


def _prioritized_missing_keys(
    state: FeedState, candidate_keys: Sequence[str]
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def add_if_missing(key: str) -> None:
        if key in seen or key not in state.papers:
            return
        seen.add(key)
        record = state.papers[key]
        doi = normalize_doi(record.doi)
        if key == f"doi:{doi}" and _missing(record.title, record.abstract):
            result.append(key)

    for key in candidate_keys:
        add_if_missing(key)
    unresolved = sorted(
        state.papers,
        key=lambda key: (-state.papers[key].published_at.timestamp(), key),
    )
    for key in unresolved:
        add_if_missing(key)
    return result


def _requested_dois(
    state: FeedState, keys: Sequence[str]
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in keys:
        record = state.papers.get(key)
        if record is None:
            continue
        doi = normalize_doi(record.doi)
        if doi and key == f"doi:{doi}" and doi not in result:
            result[doi] = key
    return result


def _missing(title: str, abstract: str) -> bool:
    cleaned = _clean_text(abstract)
    if not cleaned:
        return True
    if _title_tokens(title) == _title_tokens(cleaned):
        return True
    return _boilerplate_key(cleaned) in _BOILERPLATE


def _compatible_title(left: str, right: str) -> bool:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    left_text = " ".join(left_tokens)
    right_text = " ".join(right_tokens)
    padded_left = f" {left_text} "
    padded_right = f" {right_text} "
    if (
        left_text == right_text
        or padded_left in padded_right
        or padded_right in padded_left
    ):
        return True
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    return len(left_set & right_set) / len(left_set | right_set) >= 0.8


def _valid_abstract(title: str, abstract: object) -> str | None:
    if type(abstract) is not str:
        return None
    cleaned = _clean_text(abstract)
    if not cleaned or len(cleaned) > MAX_ABSTRACT_CHARS or _missing(title, cleaned):
        return None
    return cleaned


def _apply_values(
    state: FeedState, values: Mapping[str, str], provider: str
) -> int:
    enriched = 0
    for key, abstract in values.items():
        record = state.papers.get(key)
        if record is None or key != f"doi:{normalize_doi(record.doi)}":
            continue
        cleaned = _valid_abstract(record.title, abstract)
        if cleaned is None or not _missing(record.title, record.abstract):
            continue
        record.abstract = cleaned
        if provider not in record.sources:
            record.sources.append(provider)
        if key not in state.pending_ai:
            state.pending_ai.append(key)
        enriched += 1
    return enriched


def _openaire_dois(value: object) -> set[str]:
    if type(value) is not list:
        return set()
    result: set[str] = set()
    for pid in value:
        if type(pid) is not dict:
            continue
        if pid.get("scheme") != "doi" or type(pid.get("value")) is not str:
            continue
        doi = normalize_doi(pid["value"])
        if doi:
            result.add(doi)
    return result


def _title_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def _clean_text(value: str) -> str:
    without_markup = re.sub(r"<[^>]*>", " ", value)
    normalized = unicodedata.normalize("NFKC", without_markup)
    return " ".join(normalized.split())


def _boilerplate_key(value: str) -> str:
    return _clean_text(value).casefold().rstrip(".!?").strip()


def _report_provider_error(
    provider: str, error_class: str, status: int | None
) -> None:
    print(
        provider,
        error_class,
        status if isinstance(status, int) else "none",
        file=sys.stderr,
    )
