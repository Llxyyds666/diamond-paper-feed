"""Selection helpers for the diamond-device focus feed."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from diamond_feed.config import AppConfig
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import identity_aliases
from diamond_feed.publication import cumulative_records
from diamond_feed.render import render_rss
from diamond_feed.state import FeedState


FOCUS_LABELS = frozenset(
    {
        "diamond-power-rf-detectors",
        "diamond-thermal-management",
        "device-grade-single-crystal",
    }
)
FOCUS_RSS_NAME = "device_focus_feed.xml"

_IDENTITY_PREFIXES = frozenset({"doi", "arxiv", "figshare", "title"})


class _DuplicateKeyError(ValueError):
    """Raised when a JSON object contains a duplicate key."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _valid_identity(identity: object) -> bool:
    if not isinstance(identity, str):
        return False
    prefix, separator, value = identity.partition(":")
    return separator == ":" and prefix in _IDENTITY_PREFIXES and bool(value.strip())


def load_focus_overrides(path: Path) -> dict[str, frozenset[str]]:
    """Load strict exact-identity focus overrides, failing closed on errors."""

    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or set(raw) != {"version", "papers"}:
            raise ValueError
        if isinstance(raw["version"], bool) or raw["version"] != 1:
            raise ValueError
        papers = raw["papers"]
        if not isinstance(papers, dict):
            raise ValueError

        overrides: dict[str, frozenset[str]] = {}
        for identity, labels in papers.items():
            if not _valid_identity(identity):
                raise ValueError
            if (
                not isinstance(labels, list)
                or not labels
                or any(not isinstance(label, str) for label in labels)
                or len(labels) != len(set(labels))
                or not set(labels).issubset(FOCUS_LABELS)
            ):
                raise ValueError
            overrides[identity] = frozenset(labels)
        return overrides
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateKeyError, ValueError, TypeError) as exc:
        raise ValueError(f"invalid device focus override file: {path}") from exc


def focused_records(
    state: FeedState,
    withheld_aliases: set[str],
    overrides: dict[str, frozenset[str]],
) -> list[PaperRecord]:
    """Return deduplicated, publishable records carrying an approved focus label."""

    records: list[PaperRecord] = []
    for record in cumulative_records(state, withheld_aliases):
        labels = set(record.categories).intersection(FOCUS_LABELS)
        for alias in identity_aliases(record):
            labels.update(overrides.get(alias, ()))
        if labels:
            records.append(record)
    return records


def render_focused_rss(
    state: FeedState,
    config: AppConfig,
    withheld_aliases: set[str],
    overrides: dict[str, frozenset[str]],
) -> str:
    """Render the cumulative focus subset without the raw-feed item cap."""

    records = focused_records(state, withheld_aliases, overrides)
    feed_records = [replace(record, abstract=record.summary_zh or "") for record in records]
    return render_rss(
        feed_records,
        "Diamond Device Focus Feed · 中文摘要",
        config.publication.base_url,
        len(feed_records),
        cap_at_2000=False,
    ) + "\n"
