"""Publication policy and cumulative AI-paper selection."""

from dataclasses import replace
import json
from pathlib import Path

from diamond_feed.models import PaperRecord
from diamond_feed.normalize import group_records, identity_aliases
from diamond_feed.state import FeedState


POLICY_FIELDS = {"version", "withheld_identity_aliases"}


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
        members = [state.papers[key] for key in member_keys]
        aliases = {alias for member in members for alias in identity_aliases(member)}
        if aliases & withheld_aliases or any(member.ai_relevant is False for member in members):
            continue
        if not any(member.ai_relevant is True for member in members):
            continue
        if not (merged.summary_zh or "").strip():
            continue
        published.append(replace(merged, ai_relevant=True))
    return published
