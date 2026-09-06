"""Durable, versioned storage for collected papers."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path

from diamond_feed.models import PaperRecord


STATE_VERSION = 1


@dataclass(slots=True)
class FeedState:
    papers: dict[str, PaperRecord] = field(default_factory=dict)
    pending_ai: list[str] = field(default_factory=list)
    source_watermarks: dict[str, str] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "FeedState":
        return cls()


def _as_state(payload: object) -> FeedState:
    if not isinstance(payload, dict):
        raise ValueError("state must be a JSON object")
    if payload.get("version") != STATE_VERSION or type(payload.get("version")) is not int:
        raise ValueError(f"unsupported state version: {payload.get('version')!r}")
    papers = payload.get("papers")
    pending_ai = payload.get("pending_ai")
    source_watermarks = payload.get("source_watermarks")
    if not isinstance(papers, dict) or not isinstance(pending_ai, list) or not isinstance(source_watermarks, dict):
        raise ValueError("malformed state schema")
    if not all(isinstance(key, str) and isinstance(value, dict) for key, value in papers.items()):
        raise ValueError("malformed papers in state")
    if not all(isinstance(key, str) for key in pending_ai):
        raise ValueError("malformed pending_ai in state")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in source_watermarks.items()):
        raise ValueError("malformed source_watermarks in state")
    return FeedState(
        papers={key: PaperRecord.from_dict(value) for key, value in papers.items()},
        pending_ai=list(pending_ai),
        source_watermarks=dict(source_watermarks),
    )


def load_state(path: Path) -> FeedState:
    """Load state, returning an empty state only when no state has been saved yet."""
    if not path.exists():
        return FeedState.empty()
    try:
        return _as_state(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid state file {path}: {error}") from error


def _payload(state: FeedState) -> dict[str, object]:
    return {
        "version": STATE_VERSION,
        "papers": {key: record.to_dict() for key, record in state.papers.items()},
        "pending_ai": list(state.pending_ai),
        "source_watermarks": dict(state.source_watermarks),
    }


def save_state(path: Path, state: FeedState) -> None:
    """Atomically publish a complete state file after syncing its sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(_payload(state), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
