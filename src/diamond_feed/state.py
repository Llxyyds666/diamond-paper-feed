"""Durable, versioned storage for collected papers."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from diamond_feed.atomic import StagedFile, commit_staged, stage_text
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import record_key


STATE_VERSION = 1
ROOT_FIELDS = {"version", "papers", "pending_ai", "source_watermarks"}
PAPER_FIELDS = {
    "title",
    "abstract",
    "authors",
    "journal",
    "published_at",
    "doi",
    "url",
    "sources",
    "source_ids",
    "categories",
    "summary_zh",
    "ai_relevant",
    "ai_confidence",
}


@dataclass(slots=True)
class FeedState:
    papers: dict[str, PaperRecord] = field(default_factory=dict)
    pending_ai: list[str] = field(default_factory=list)
    source_watermarks: dict[str, str] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "FeedState":
        return cls()


def _aware_datetime(value: object, field_name: str) -> datetime:
    _persisted_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be a parseable ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _persisted_string(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field_name} contains an unpaired Unicode surrogate") from error
    return value


def _string_list(value: object, field_name: str) -> list[str]:
    if type(value) is not list or not all(type(item) is str for item in value):
        raise ValueError(f"{field_name} must be a JSON array of strings")
    for item in value:
        _persisted_string(item, field_name)
    return list(value)


def _paper_from_payload(payload: object) -> PaperRecord:
    if type(payload) is not dict or set(payload) != PAPER_FIELDS:
        raise ValueError("paper must contain the exact schema fields")
    for field_name in ("title", "abstract", "journal", "url"):
        _persisted_string(payload[field_name], f"paper {field_name}")
    for field_name in ("authors", "sources", "source_ids", "categories"):
        _string_list(payload[field_name], f"paper {field_name}")
    for field_name in ("doi", "summary_zh"):
        if payload[field_name] is not None:
            _persisted_string(payload[field_name], f"paper {field_name}")
    if payload["ai_relevant"] is not None and type(payload["ai_relevant"]) is not bool:
        raise ValueError("paper ai_relevant must be a boolean or null")
    confidence = payload["ai_confidence"]
    if confidence is not None:
        if type(confidence) not in (int, float):
            raise ValueError("paper ai_confidence must be a finite number or null")
        try:
            finite_confidence = math.isfinite(confidence)
        except OverflowError as error:
            raise ValueError("paper ai_confidence must be a finite number or null") from error
        if not finite_confidence:
            raise ValueError("paper ai_confidence must be a finite number or null")
    published_at = _aware_datetime(payload["published_at"], "paper published_at")
    return PaperRecord(
        title=payload["title"],
        abstract=payload["abstract"],
        authors=list(payload["authors"]),
        journal=payload["journal"],
        published_at=published_at,
        doi=payload["doi"],
        url=payload["url"],
        sources=list(payload["sources"]),
        source_ids=list(payload["source_ids"]),
        categories=list(payload["categories"]),
        summary_zh=payload["summary_zh"],
        ai_relevant=payload["ai_relevant"],
        ai_confidence=None if confidence is None else float(confidence),
    )


def _validate_state(state: FeedState) -> None:
    if type(state.papers) is not dict or not all(type(key) is str and isinstance(value, PaperRecord) for key, value in state.papers.items()):
        raise ValueError("papers must map string keys to PaperRecord values")
    for key, record in state.papers.items():
        _persisted_string(key, "paper key")
        validated_record = _paper_from_payload(_paper_payload(record))
        if key != record_key(validated_record):
            raise ValueError(f"paper key is not canonical: {key}")
    if type(state.pending_ai) is not list or not all(type(key) is str for key in state.pending_ai):
        raise ValueError("pending_ai must be a list of strings")
    for key in state.pending_ai:
        _persisted_string(key, "pending_ai key")
    if len(state.pending_ai) != len(set(state.pending_ai)):
        raise ValueError("pending_ai keys must be unique")
    if not set(state.pending_ai) <= set(state.papers):
        raise ValueError("pending_ai keys must exist in papers")
    if type(state.source_watermarks) is not dict or not all(type(key) is str for key in state.source_watermarks):
        raise ValueError("source_watermarks must map string keys to ISO datetimes")
    for key, value in state.source_watermarks.items():
        _persisted_string(key, "source watermark key")
        _aware_datetime(value, "source watermark")


def _as_state(payload: object) -> FeedState:
    if type(payload) is not dict:
        raise ValueError("state must be a JSON object")
    if payload.get("version") != STATE_VERSION or type(payload.get("version")) is not int:
        raise ValueError(f"unsupported state version: {payload.get('version')!r}")
    if set(payload) != ROOT_FIELDS:
        raise ValueError("state must contain the exact schema fields")
    papers = payload.get("papers")
    pending_ai = payload.get("pending_ai")
    source_watermarks = payload.get("source_watermarks")
    if type(papers) is not dict or type(pending_ai) is not list or type(source_watermarks) is not dict:
        raise ValueError("malformed state schema")
    normalized_watermarks = {}
    for key, value in source_watermarks.items():
        if type(key) is not str:
            continue
        _persisted_string(key, "source watermark key")
        normalized_watermarks[key] = _aware_datetime(value, "source watermark").isoformat()
    if len(normalized_watermarks) != len(source_watermarks):
        raise ValueError("source watermark keys must be strings")
    state = FeedState(
        papers={key: _paper_from_payload(value) for key, value in papers.items() if type(key) is str},
        pending_ai=list(pending_ai),
        source_watermarks=normalized_watermarks,
    )
    if len(state.papers) != len(papers):
        raise ValueError("paper keys must be strings")
    _validate_state(state)
    return state


def load_state(path: Path) -> FeedState:
    """Load state, returning an empty state only when no state has been saved yet."""
    if not path.exists():
        return FeedState.empty()
    try:
        return _as_state(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid state file {path}: {error}") from error


def _payload(state: FeedState) -> dict[str, object]:
    _validate_state(state)
    return {
        "version": STATE_VERSION,
        "papers": {key: _paper_payload(record) for key, record in state.papers.items()},
        "pending_ai": list(state.pending_ai),
        "source_watermarks": {
            key: _aware_datetime(value, "source watermark").isoformat()
            for key, value in state.source_watermarks.items()
        },
    }


def _paper_payload(record: PaperRecord) -> dict[str, object]:
    return {
        "title": record.title,
        "abstract": record.abstract,
        "authors": list(record.authors) if type(record.authors) is list else record.authors,
        "journal": record.journal,
        "published_at": record.published_at.astimezone(timezone.utc).isoformat()
        if isinstance(record.published_at, datetime) and record.published_at.tzinfo is not None and record.published_at.utcoffset() is not None
        else record.published_at,
        "doi": record.doi,
        "url": record.url,
        "sources": list(record.sources) if type(record.sources) is list else record.sources,
        "source_ids": list(record.source_ids) if type(record.source_ids) is list else record.source_ids,
        "categories": list(record.categories) if type(record.categories) is list else record.categories,
        "summary_zh": record.summary_zh,
        "ai_relevant": record.ai_relevant,
        "ai_confidence": record.ai_confidence,
    }


def stage_state(path: Path, state: FeedState) -> StagedFile:
    """Validate and durably stage state without replacing the last good file."""
    contents = json.dumps(_payload(state), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    return stage_text(path, contents)


def save_state(path: Path, state: FeedState) -> None:
    """Atomically publish a validated state file."""
    commit_staged([stage_state(path, state)])
