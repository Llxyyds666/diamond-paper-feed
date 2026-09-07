"""Collection orchestration and command-line entry point."""

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import io
from pathlib import Path
import sys
from typing import Callable, Iterable

from diamond_feed.atomic import CommitResult, atomic_write_text, commit_staged, discard_staged, raise_with_cleanup, stage_text, validate_output_layout
from diamond_feed.config import load_config
from diamond_feed.filtering import QueryRules, load_rules, matches_rules
from diamond_feed.http import FetchError, fetch_bytes
from diamond_feed.models import PaperRecord, SourceFailure
from diamond_feed.normalize import merge_records, record_key
from diamond_feed.render import render_rss
from diamond_feed.sources import arxiv, crossref, openalex
from diamond_feed.sources.rss import collect_rss
from diamond_feed.state import FeedState, SourceContinuation, load_state, stage_state


@dataclass(frozen=True, slots=True)
class CollectionStats:
    added: int = 0
    merged: int = 0
    filtered: int = 0


@dataclass(frozen=True, slots=True)
class ScholarlyHarvest:
    source_name: str
    records: list[PaperRecord]
    failure: SourceFailure | None
    continuation: SourceContinuation | None
    complete: bool
    pages_fetched: int


def merge_into_state(state: FeedState, incoming: Iterable[PaperRecord], rules: QueryRules) -> CollectionStats:
    """Filter and merge records while preserving the oldest-first AI work queue."""
    added = merged = filtered = 0
    for record in incoming:
        if not matches_rules(record, rules):
            filtered += 1
            continue
        key = record_key(record)
        current = state.papers.get(key)
        if current is None:
            state.papers[key] = record
            added += 1
            if key not in state.pending_ai:
                state.pending_ai.append(key)
            continue

        was_empty = not current.abstract.strip()
        state.papers[key] = merge_records(current, record)
        merged += 1
        is_processed = current.ai_relevant is not None
        if is_processed and was_empty and state.papers[key].abstract.strip() and key not in state.pending_ai:
            state.pending_ai.append(key)

    state.pending_ai.sort(key=lambda key: (state.papers[key].published_at, key))
    return CollectionStats(added=added, merged=merged, filtered=filtered)


def _read_sources(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None or "url" not in reader.fieldnames:
            raise ValueError("RSS source TSV must include a url column")
        return [dict(row) for row in reader if row.get("url", "").strip()]


def _watermark_date(state: FeedState, source: str, now: datetime, lookback_days: int) -> date:
    value = state.source_watermarks.get(source)
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).date()
        except ValueError:
            pass
    return (now - timedelta(days=lookback_days)).date()


def _failure(name: str, error: Exception) -> SourceFailure:
    category = error.category if isinstance(error, FetchError) else "network_error"
    detail = error.detail if isinstance(error, FetchError) else str(error)
    return SourceFailure(datetime.now(timezone.utc), category, name, detail)


def _collect_scholarly(
    name: str,
    query: str,
    from_date: date,
    fetcher: Callable[[str], object],
    rows: int,
    continuation: SourceContinuation | None = None,
) -> ScholarlyHarvest:
    module = {"openalex": openalex, "crossref": crossref, "arxiv": arxiv}[name]
    limit = min(rows, 2000)
    original_from_date = continuation.from_date if continuation is not None else from_date
    cursor = continuation.cursor if continuation is not None else "*"
    records: list[PaperRecord] = []
    pages_fetched = 0

    if name == "arxiv":
        url = module.build_url(query, original_from_date, limit)
        try:
            result = fetcher(url)
            status = getattr(result, "status")
            if not 200 <= status < 300:
                failure = SourceFailure(datetime.now(timezone.utc), f"http_{status}", url, f"HTTP {status}")
                return ScholarlyHarvest(name, [], failure, None, False, 0)
            return ScholarlyHarvest(name, module.parse_response(getattr(result, "body")), None, None, True, 1)
        except Exception as error:
            return ScholarlyHarvest(name, [], _failure(url, error), None, False, 0)

    page_limit = {"openalex": 200, "crossref": 1000}[name]
    while len(records) < limit:
        requested_rows = min(limit - len(records), page_limit)
        url = module.build_url(query, original_from_date, requested_rows, cursor)
        try:
            result = fetcher(url)
            status = getattr(result, "status")
            if not 200 <= status < 300:
                failure = SourceFailure(datetime.now(timezone.utc), f"http_{status}", url, f"HTTP {status}")
                return ScholarlyHarvest(
                    name,
                    records,
                    failure,
                    SourceContinuation(original_from_date, cursor),
                    False,
                    pages_fetched,
                )
            page = module.parse_page(getattr(result, "body"))
        except Exception as error:
            return ScholarlyHarvest(
                name,
                records,
                _failure(url, error),
                SourceContinuation(original_from_date, cursor),
                False,
                pages_fetched,
            )

        pages_fetched += 1
        records.extend(page.records)
        if page.next_cursor is None or (name == "crossref" and page.item_count < requested_rows):
            return ScholarlyHarvest(name, records, None, None, True, pages_fetched)
        if page.next_cursor == cursor:
            failure = SourceFailure(
                datetime.now(timezone.utc),
                "parse_error",
                url,
                "pagination cursor did not advance",
            )
            return ScholarlyHarvest(
                name,
                records,
                failure,
                SourceContinuation(original_from_date, cursor),
                False,
                pages_fetched,
            )
        cursor = page.next_cursor

    return ScholarlyHarvest(
        name,
        records,
        None,
        SourceContinuation(original_from_date, cursor),
        False,
        pages_fetched,
    )


def _write_failures(path: Path, failures: list[SourceFailure]) -> CommitResult:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(["timestamp", "category", "url", "detail"])
    for failure in failures:
        writer.writerow([failure.timestamp.astimezone(timezone.utc).isoformat(), failure.category, failure.url, failure.detail])
    return atomic_write_text(path, output.getvalue())


def _report_cleanup_debt(result: CommitResult) -> None:
    if not result.cleanup_pending:
        return
    print(f"publication status: {result.status}", file=sys.stderr)
    for error in result.cleanup_errors:
        print(f"publication cleanup debt: {error}", file=sys.stderr)


def main(argv: list[str] | None = None, *, now: Callable[[], datetime] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect diamond literature into a raw RSS feed.")
    parser.add_argument("--config", type=Path, default=Path("paper_feed_config.json"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--sources", type=Path, default=Path("config/rss_sources.tsv"))
    parser.add_argument("--queries", type=Path, default=Path("config/queries.json"))
    parser.add_argument("--feed", type=Path, default=Path("filtered_feed.xml"))
    parser.add_argument("--failures", type=Path, default=Path("fetch_failures.tsv"))
    args = parser.parse_args(argv)
    validate_output_layout((args.state, args.feed, args.failures))
    config = load_config(args.config)
    rules = load_rules(args.queries)
    state = load_state(args.state)
    current_time = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    fetcher = lambda url: fetch_bytes(url, config.collection.http_timeout_seconds, config.collection.http_attempts)
    records: list[PaperRecord] = []
    failures: list[SourceFailure] = []
    successful_sources: list[str] = []
    scholarly_progress = False

    for row in _read_sources(args.sources):
        url = row["url"].strip()
        collected, failure = collect_rss(url, fetcher)
        source_name = f"rss:{url}"
        if failure is None:
            records.extend(collected)
            successful_sources.append(source_name)
        else:
            failures.append(failure)

    query = rules.include_any[0] if rules.include_any else "diamond"
    for name in ("openalex", "crossref", "arxiv"):
        continuation = state.source_continuations.get(name)
        from_date = continuation.from_date if continuation is not None else _watermark_date(
            state, name, current_time, config.collection.lookback_days
        )
        harvest = _collect_scholarly(
            name,
            query,
            from_date,
            fetcher,
            config.collection.raw_feed_max_items,
            continuation,
        )
        records.extend(harvest.records)
        if harvest.failure is not None:
            failures.append(harvest.failure)
        if harvest.complete:
            state.source_continuations.pop(name, None)
            successful_sources.append(name)
            scholarly_progress = True
        elif harvest.pages_fetched:
            if harvest.continuation is None:
                raise RuntimeError(f"{name} pagination stopped without a continuation")
            state.source_continuations[name] = harvest.continuation
            state.source_watermarks.pop(name, None)
            scholarly_progress = True

    failure_result = _write_failures(args.failures, failures)
    _report_cleanup_debt(failure_result)
    if not successful_sources and not scholarly_progress:
        return 2

    merge_into_state(state, records, rules)
    for source_name in successful_sources:
        state.source_watermarks[source_name] = current_time.isoformat()
    xml = render_rss(list(state.papers.values()), config.publication.title, config.publication.base_url, config.collection.raw_feed_max_items)
    staged = []
    try:
        staged.append(stage_state(args.state, state))
        staged.append(stage_text(args.feed, xml))
    except Exception as staging_error:
        raise_with_cleanup(
            staging_error,
            "stage collection outputs",
            discard_staged(staged),
        )
    publication_result = commit_staged(staged)
    _report_cleanup_debt(publication_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
