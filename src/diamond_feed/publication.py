"""Publication policy and cumulative AI-paper selection."""

from dataclasses import replace
import json
from pathlib import Path

from diamond_feed.models import PaperRecord
from diamond_feed.normalize import (
    group_records,
    identity_aliases,
    merge_records,
    normalize_doi,
    record_key,
)
from diamond_feed.state import FeedState


POLICY_FIELDS = {"version", "withheld_identity_aliases"}
REPOSITORY_ARTIFACT_DOI_PREFIXES = (
    "10.17172/nomad.",
    "10.24435/materialscloud",
    "10.25439/rmt.",
    "10.25446/oxford.",
    "10.4121/",
    "10.57760/sciencedb.",
    "10.6084/m9.figshare.",
    "10.60893/figshare.",
    "10.71947/arim.",
    "10.82901/nemar.",
)


def is_repository_artifact(record: PaperRecord) -> bool:
    """Return whether a record is a repository asset rather than a paper."""
    doi = normalize_doi(record.doi) or ""
    return doi.startswith(REPOSITORY_ARTIFACT_DOI_PREFIXES)


def quarantine_repository_artifacts(state: FeedState) -> int:
    """Reject persisted repository assets and remove them from the AI queue."""
    artifact_keys = {
        key for key, record in state.papers.items() if is_repository_artifact(record)
    }
    changed = 0
    for key in artifact_keys:
        record = state.papers[key]
        if record.ai_relevant is not False:
            record.ai_relevant = False
            changed += 1
    pending = [key for key in state.pending_ai if key not in artifact_keys]
    if pending != state.pending_ai:
        state.pending_ai = pending
        changed += 1
    return changed


def load_withheld_aliases(path: Path) -> set[str]:
    """Load the exact publication-policy schema; an absent policy withholds nothing."""
    path = Path(path)
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid publication policy {path}") from error
    if (
        type(payload) is not dict
        or set(payload) != POLICY_FIELDS
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or type(payload["withheld_identity_aliases"]) is not list
        or not all(
            type(alias) is str and bool(alias)
            for alias in payload["withheld_identity_aliases"]
        )
    ):
        raise ValueError(f"invalid publication policy {path}")
    return set(payload["withheld_identity_aliases"])


def cumulative_records(state: FeedState, withheld_aliases: set[str]) -> list[PaperRecord]:
    """Return merged accepted records without changing the state or its records."""
    records = list(state.papers.values())
    published: list[PaperRecord] = []
    for merged, member_keys in group_records(records).values():
        if is_repository_artifact(merged):
            continue
        members = [state.papers[key] for key in member_keys]
        aliases = {alias for member in members for alias in identity_aliases(member)}
        if aliases & withheld_aliases or any(member.ai_relevant is False for member in members):
            continue
        if not any(member.ai_relevant is True for member in members):
            continue
        if not (merged.summary_zh or "").strip():
            continue
        published.append(replace(merged, ai_relevant=True))
    deduplicated: dict[str, PaperRecord] = {}
    for record in published:
        title_year_key = record_key(replace(record, doi=None))
        existing = deduplicated.get(title_year_key)
        deduplicated[title_year_key] = (
            record
            if existing is None
            else replace(merge_records(existing, record), ai_relevant=True)
        )
    return list(deduplicated.values())
