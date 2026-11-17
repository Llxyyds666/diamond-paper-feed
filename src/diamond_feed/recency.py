"""Publication-date eligibility for current collection and notification work."""

from datetime import date, datetime, timedelta, timezone

from diamond_feed.models import PaperRecord
from diamond_feed.normalize import group_records
from diamond_feed.state import FeedState


def _utc_date(value: datetime) -> date:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("publication window timestamps must include a timezone")
    return value.astimezone(timezone.utc).date()


def is_recent_publication(record: PaperRecord, now: datetime, lookback_days: int = 30) -> bool:
    """Require a real publication date in the rolling lookback, not a recent index update.

    Future issue dates remain eligible: already-online articles can have a later
    print year. An RSS fallback timestamp flagged ``missing_date`` is not evidence
    that an article was published recently.
    """
    lower_bound = _utc_date(now) - timedelta(days=lookback_days)
    return "missing_date" not in record.categories and _utc_date(record.published_at) >= lower_bound


def prune_stale_pending(state: FeedState, now: datetime, lookback_days: int = 30) -> int:
    """Apply eligibility to the same merged representatives the AI will receive."""
    original_count = len(state.pending_ai)
    eligible_aliases = {
        alias
        for record, aliases in group_records(list(state.papers.values())).values()
        if is_recent_publication(record, now, lookback_days)
        for alias in aliases
    }
    state.pending_ai = [
        key for key in state.pending_ai
        if key in eligible_aliases
    ]
    return original_count - len(state.pending_ai)
