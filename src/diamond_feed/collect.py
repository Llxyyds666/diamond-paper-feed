"""Collection orchestration and command-line entry point."""

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from diamond_feed.config import load_config
from diamond_feed.filtering import QueryRules, load_rules, matches_rules
from diamond_feed.http import FetchError, fetch_bytes
from diamond_feed.models import PaperRecord, SourceFailure
from diamond_feed.normalize import merge_records, record_key
from diamond_feed.render import render_rss
from diamond_feed.sources import arxiv, crossref, openalex
from diamond_feed.sources.rss import collect_rss
from diamond_feed.state import FeedState, load_state, save_state


@dataclass(frozen=True, slots=True)
class CollectionStats:
    added: int = 0
    merged: int = 0
    filtered: int = 0


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
) -> list[tuple[str, list[PaperRecord], SourceFailure | None]]:
    module = {"openalex": openalex, "crossref": crossref, "arxiv": arxiv}[name]
    url = module.build_url(query, from_date, rows)
    try:
        result = fetcher(url)
        status = getattr(result, "status")
        if not 200 <= status < 300:
            return [(name, [], SourceFailure(datetime.now(timezone.utc), f"http_{status}", url, f"HTTP {status}"))]
        return [(name, module.parse_response(getattr(result, "body")), None)]
    except Exception as error:
        return [(name, [], _failure(url, error))]


def _write_failures(path: Path, failures: list[SourceFailure]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["timestamp", "category", "url", "detail"])
        for failure in failures:
            writer.writerow([failure.timestamp.astimezone(timezone.utc).isoformat(), failure.category, failure.url, failure.detail])


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            __import__("os").fsync(handle.fileno())
        __import__("os").replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None, *, now: Callable[[], datetime] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect diamond literature into a raw RSS feed.")
    parser.add_argument("--config", type=Path, default=Path("paper_feed_config.json"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--sources", type=Path, default=Path("config/rss_sources.tsv"))
    parser.add_argument("--queries", type=Path, default=Path("config/queries.json"))
    parser.add_argument("--feed", type=Path, default=Path("filtered_feed.xml"))
    parser.add_argument("--failures", type=Path, default=Path("fetch_failures.tsv"))
    args = parser.parse_args(argv)
    config = load_config(args.config)
    rules = load_rules(args.queries)
    state = load_state(args.state)
    current_time = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    fetcher = lambda url: fetch_bytes(url, config.collection.http_timeout_seconds, config.collection.http_attempts)
    records: list[PaperRecord] = []
    failures: list[SourceFailure] = []
    successful_sources: list[str] = []

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
        from_date = _watermark_date(state, name, current_time, config.collection.lookback_days)
        for source_name, collected, failure in _collect_scholarly(name, query, from_date, fetcher, config.collection.raw_feed_max_items):
            if failure is None:
                records.extend(collected)
                successful_sources.append(source_name)
            else:
                failures.append(failure)

    _write_failures(args.failures, failures)
    if not successful_sources:
        return 2

    merge_into_state(state, records, rules)
    for source_name in successful_sources:
        state.source_watermarks[source_name] = current_time.isoformat()
    xml = render_rss(list(state.papers.values()), config.publication.title, config.publication.base_url, config.collection.raw_feed_max_items)
    _atomic_write_text(args.feed, xml)
    save_state(args.state, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
