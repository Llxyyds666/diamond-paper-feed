from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from diamond_feed.normalize import record_key
from diamond_feed.state import FeedState


NOW = datetime(2026, 11, 17, 0, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("published_at", "expected"),
    [
        (datetime(2015, 9, 1, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 17, 23, 59, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 18, tzinfo=timezone.utc), True),
        (datetime(2026, 11, 17, tzinfo=timezone.utc), True),
        (datetime(2027, 1, 1, tzinfo=timezone.utc), True),
    ],
)
def test_publication_window_uses_inclusive_calendar_days_and_allows_future_issues(
    diamond_records, published_at, expected
):
    from diamond_feed.recency import is_recent_publication

    record = replace(diamond_records[0], published_at=published_at)

    assert is_recent_publication(record, NOW) is expected


def test_publication_window_compares_utc_dates(diamond_records):
    from diamond_feed.recency import is_recent_publication

    record = replace(
        diamond_records[0],
        published_at=datetime(2026, 10, 18, 0, 30, tzinfo=timezone(timedelta(hours=8))),
    )
    local_now = NOW.astimezone(timezone(timedelta(hours=-6)))

    assert is_recent_publication(record, local_now) is False


def test_missing_date_is_not_treated_as_a_new_publication(diamond_records):
    from diamond_feed.recency import is_recent_publication

    record = replace(diamond_records[0], published_at=NOW, categories=["missing_date"])

    assert is_recent_publication(record, NOW) is False


def test_publication_window_honors_configured_lookback(diamond_records):
    from diamond_feed.recency import is_recent_publication

    record = replace(diamond_records[0], published_at=NOW - timedelta(days=60))

    assert is_recent_publication(record, NOW, 30) is False
    assert is_recent_publication(record, NOW, 90) is True


def test_publication_window_requires_timezone_aware_timestamps(diamond_records):
    from diamond_feed.recency import is_recent_publication

    with pytest.raises(ValueError, match="timezone"):
        is_recent_publication(diamond_records[0], NOW.replace(tzinfo=None))


def test_prune_stale_pending_preserves_historical_records_and_recent_queue_order(diamond_records):
    from diamond_feed.recency import prune_stale_pending

    old = replace(diamond_records[0], published_at=datetime(2015, 9, 1, tzinfo=timezone.utc))
    recent = replace(diamond_records[1], published_at=NOW)
    missing = replace(recent, doi="10.1000/missing", categories=["missing_date"])
    papers = {record_key(record): record for record in (old, recent, missing)}
    state = FeedState(papers=papers.copy(), pending_ai=list(papers), source_watermarks={})

    assert prune_stale_pending(state, NOW) == 2
    assert state.pending_ai == [record_key(recent)]
    assert state.papers == papers
    assert prune_stale_pending(state, NOW) == 0


@pytest.mark.parametrize("canonical_is_recent", [False, True])
def test_prune_stale_pending_checks_merged_alias_representative(diamond_records, canonical_is_recent):
    from diamond_feed.recency import prune_stale_pending

    old_date = datetime(2015, 1, 1, tzinfo=timezone.utc)
    recent_date = NOW - timedelta(days=1)
    canonical = replace(
        diamond_records[0],
        doi="10.48550/arxiv.1501.00001",
        url="https://arxiv.org/abs/1501.00001",
        published_at=recent_date if canonical_is_recent else old_date,
    )
    version = replace(
        canonical,
        doi="10.48550/arxiv.1501.00001v2",
        url="https://arxiv.org/abs/1501.00001v2",
        published_at=old_date if canonical_is_recent else recent_date,
    )
    unrelated_recent = replace(diamond_records[1], published_at=recent_date)
    papers = {record_key(record): record for record in (canonical, version, unrelated_recent)}
    pending = [record_key(version), record_key(unrelated_recent)]
    state = FeedState(papers=papers.copy(), pending_ai=pending.copy(), source_watermarks={})

    assert prune_stale_pending(state, NOW) == (0 if canonical_is_recent else 1)
    assert state.pending_ai == (pending if canonical_is_recent else [record_key(unrelated_recent)])
    assert state.papers == papers
