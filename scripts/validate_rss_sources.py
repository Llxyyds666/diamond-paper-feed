"""Validate RSS registry entries with real GET requests and curate hard failures."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import io
from pathlib import Path
import re
from typing import Callable

import feedparser

from diamond_feed.atomic import commit_staged, stage_text, validate_output_layout
from diamond_feed.http import FetchError, HttpResult, fetch_bytes
if __package__:
    from .import_rss_sources import SOURCE_COLUMNS, SourceRow, normalize_url
else:
    from import_rss_sources import SOURCE_COLUMNS, SourceRow, normalize_url


FAILURE_COLUMNS = ("timestamp", "category", "url", "detail")
HARD_FAILURES = frozenset({"http_404", "http_410", "parse_error", "retired"})
SOFT_FAILURES = frozenset({"timeout", "url_error", "network_error", "empty_feed"})
DATABASE_ONLY_COVERAGE = {
    "ACS Applied Materials & Interfaces": "official RSS returned HTTP 403",
    "ACS Nano": "official RSS returned HTTP 403",
    "American Mineralogist": "no stable official RSS endpoint found",
    "Applied Physics Letters": "official RSS endpoint is retired (HTTP 404)",
    "Carbon": "official RSS did not survive bounded GET validation",
    "Carbon Trends": "official RSS did not survive bounded GET validation",
    "Crystal Growth & Design": "official RSS returned HTTP 403",
    "Diamond and Related Materials": "official RSS did not survive bounded GET validation",
    "Earth and Planetary Science Letters": "official RSS did not survive bounded GET validation",
    "Journal of Applied Physics": "official RSS endpoint is retired (HTTP 404)",
    "Journal of Carbon Research": "official RSS endpoint returned HTTP 404",
    "Journal of Physics D: Applied Physics": "official RSS redirects to a bot challenge",
    "Nano Letters": "official RSS returned HTTP 403",
    "Physics of the Earth and Planetary Interiors": "official RSS did not survive bounded GET validation",
    "Quantum Science and Technology": "no stable official RSS restoration path is verified",
    "Semiconductor Science and Technology": "official RSS redirects to a bot challenge",
    "Surface and Coatings Technology": "official RSS did not survive bounded GET validation",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FailureRow:
    timestamp: str
    category: str
    url: str
    detail: str


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    total: int
    active: int
    hard: int
    soft: int


def _safe_field(value: object, *, fallback: str) -> str:
    cleaned = _SPACE.sub(" ", _CONTROL.sub(" ", str(value))).strip()
    return cleaned or fallback


def _read_sources(path: Path) -> list[SourceRow]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if tuple(reader.fieldnames or ()) != SOURCE_COLUMNS:
            raise ValueError(f"RSS source TSV must have exact columns: {', '.join(SOURCE_COLUMNS)}")
        by_url: dict[str, SourceRow] = {}
        for raw in reader:
            url = normalize_url(raw["url"])
            if url in by_url:
                continue
            by_url[url] = SourceRow(
                _safe_field(raw["name"], fallback="unknown"),
                _safe_field(raw["category"], fallback="unknown"),
                url,
            )
    return sorted(by_url.values(), key=lambda row: row.url)


def _render_sources(rows: list[SourceRow]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(SOURCE_COLUMNS)
    for row in sorted(rows, key=lambda item: (item.name.casefold(), item.category.casefold(), item.url)):
        writer.writerow((row.name, row.category, row.url))
    return output.getvalue()


def _render_failures(rows: list[FailureRow]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(FAILURE_COLUMNS)
    for row in sorted(rows, key=lambda item: (item.url, item.category, item.detail)):
        writer.writerow((row.timestamp, row.category, row.url, row.detail))
    return output.getvalue()


def _inspect(
    url: str, fetcher: Callable[[str], HttpResult]
) -> tuple[str | None, int | None, str, str]:
    try:
        result = fetcher(url)
    except FetchError as error:
        return error.category, error.status, url, _safe_field(error.detail, fallback=error.category)
    except TimeoutError:
        return "timeout", None, url, "timeout"
    except Exception as error:
        return "network_error", None, url, _safe_field(error, fallback="network_error")

    # Failure rows identify the configured source, not an unstable captcha/cookie redirect.
    final_url = url
    if not 200 <= result.status < 300:
        return f"http_{result.status}", result.status, final_url, f"HTTP {result.status}"
    parsed = feedparser.parse(result.body)
    if parsed.bozo:
        return "parse_error", result.status, final_url, "malformed feed"
    if not parsed.entries:
        return "empty_feed", result.status, final_url, "feed contained no entries"
    title = _safe_field(parsed.feed.get("title", ""), fallback="unknown")
    return None, result.status, final_url, title


def validate_registry(
    sources_path: Path,
    failures_path: Path,
    *,
    fetcher: Callable[[str], HttpResult],
    now: Callable[[], datetime] | None = None,
    confirm_hard: bool = True,
    workers: int = 1,
    reporter: Callable[[str], None] | None = None,
) -> ValidationSummary:
    """Validate, enrich names, retain soft failures, and atomically publish both TSVs."""
    rows = _read_sources(sources_path)
    clock = now or (lambda: datetime.now(timezone.utc))
    timestamp = clock().astimezone(timezone.utc).isoformat()
    active: list[SourceRow] = []
    failures: list[FailureRow] = []
    hard_count = soft_count = 0

    if workers < 1:
        raise ValueError("workers must be at least 1")

    def inspect_row(row: SourceRow) -> tuple[str | None, int | None, str, str]:
        category, status, final_url, detail = _inspect(row.url, fetcher)
        if category in HARD_FAILURES and confirm_hard:
            category, status, final_url, detail = _inspect(row.url, fetcher)
        return category, status, final_url, detail

    if workers == 1:
        inspections = map(inspect_row, rows)
    else:
        executor = ThreadPoolExecutor(max_workers=workers)
        inspections = executor.map(inspect_row, rows)

    try:
        paired = zip(rows, inspections, strict=True)
        for row, (category, status, final_url, detail) in paired:
            if category is None:
                title = detail if row.name == "unknown" else row.name
                active.append(SourceRow(_safe_field(title, fallback="unknown"), row.category, row.url))
                outcome = "ok"
            else:
                safe_category = _safe_field(category, fallback="network_error")
                failures.append(
                    FailureRow(
                        timestamp,
                        safe_category,
                        _safe_field(final_url, fallback=row.url),
                        _safe_field(detail, fallback=safe_category),
                    )
                )
                if safe_category in HARD_FAILURES:
                    hard_count += 1
                    outcome = f"hard:{safe_category}"
                else:
                    soft_count += 1
                    active.append(row)
                    outcome = f"soft:{safe_category}"
            if reporter is not None:
                rendered_status = str(status) if status is not None else "none"
                reporter(f"{row.url}\tstatus={rendered_status}\tcategory={outcome}")
    finally:
        if workers != 1:
            executor.shutdown(wait=True)

    validate_output_layout((sources_path, failures_path))
    commit_staged(
        (
            stage_text(sources_path, _render_sources(active)),
            stage_text(failures_path, _render_failures(failures)),
        )
    )
    return ValidationSummary(len(rows), len(active), hard_count, soft_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and curate journal RSS sources.")
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--failures", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    fetcher = lambda url: fetch_bytes(url, timeout=args.timeout, attempts=args.attempts)
    summary = validate_registry(
        args.sources,
        args.failures,
        fetcher=fetcher,
        confirm_hard=True,
        workers=args.workers,
        reporter=print,
    )
    print(
        f"validation total={summary.total} active={summary.active} "
        f"hard={summary.hard} soft={summary.soft}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
