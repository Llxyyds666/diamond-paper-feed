"""Bounded daily AI screening and transactional Chinese digest publication."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
from typing import Protocol, Sequence
from urllib.parse import urlsplit

from diamond_feed.ai import CATEGORIES, DeepSeekClient, RequestBudget, screen_batch
from diamond_feed.atomic import (
    StagedFile,
    commit_staged,
    discard_staged,
    raise_with_cleanup,
    stage_text,
    validate_output_layout,
)
from diamond_feed.config import AppConfig, load_config
from diamond_feed.models import AiDecision, PaperRecord
from diamond_feed.render import render_rss
from diamond_feed.state import FeedState, load_state, stage_state


RSS_NAME = "ai_summary_feed.xml"
HTML_NAME = "ai_summary.html"
USAGE_NAME = "ai_usage.json"
USAGE_NUMERIC_FIELDS = (
    "candidates",
    "processed",
    "selected",
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)
USAGE_FIELDS = {"date", *USAGE_NUMERIC_FIELDS}

CATEGORY_TITLES = {
    "growth-processing": "生长与加工",
    "films-membranes": "薄膜与膜材料",
    "doping-defects": "掺杂与缺陷",
    "quantum-color-centers": "量子与色心",
    "electronics-optoelectronics": "电子与光电器件",
    "thermal-mechanical-acoustic": "热、力与声学",
    "piezoelectric-sensing": "压电与传感",
    "electrochemistry-catalysis": "电化学与催化",
    "nanodiamond-biomedical": "纳米金刚石与生物医学",
    "natural-diamond-geoscience": "天然金刚石与地球科学",
    "adjacent-dlc": "类金刚石及相关材料",
    "other-diamond": "其他金刚石研究",
}


class JsonClient(Protocol):
    def complete_json(
        self,
        messages: Sequence[dict[str, object]],
        max_tokens: int,
        budget: RequestBudget,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class SummaryStats:
    candidates: int
    processed: int
    selected: int
    requests: int
    remaining: int
    failed: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def usage_entry(self, day: str) -> dict[str, int | str]:
        return {
            "date": day,
            **{field: getattr(self, field) for field in USAGE_NUMERIC_FIELDS},
        }


def _strict_nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _client_token_totals(client: object) -> tuple[int, int]:
    prompt = _strict_nonnegative_int(getattr(client, "prompt_tokens", None))
    completion = _strict_nonnegative_int(getattr(client, "completion_tokens", None))
    if prompt is not None and completion is not None:
        return prompt, completion
    usage = getattr(client, "usage", None)
    if type(usage) is dict:
        prompt = _strict_nonnegative_int(usage.get("prompt_tokens"))
        completion = _strict_nonnegative_int(usage.get("completion_tokens"))
        if prompt is not None and completion is not None:
            return prompt, completion
    return 0, 0


def _usage_entries(path: Path) -> list[dict[str, int | str]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid AI usage file {path}") from error
    if type(raw) is not list:
        raise ValueError(f"invalid AI usage file {path}")
    entries: list[dict[str, int | str]] = []
    seen_dates: set[str] = set()
    for item in raw:
        if type(item) is not dict or set(item) != USAGE_FIELDS or type(item["date"]) is not str:
            raise ValueError(f"invalid AI usage file {path}")
        try:
            date.fromisoformat(item["date"])
        except ValueError as error:
            raise ValueError(f"invalid AI usage file {path}") from error
        if item["date"] in seen_dates:
            raise ValueError(f"invalid AI usage file {path}")
        seen_dates.add(item["date"])
        if any(
            _strict_nonnegative_int(item[field]) is None
            for field in USAGE_NUMERIC_FIELDS
        ):
            raise ValueError(f"invalid AI usage file {path}")
        if item["total_tokens"] != item["prompt_tokens"] + item["completion_tokens"]:
            raise ValueError(f"invalid AI usage file {path}")
        entries.append(dict(item))
    return entries


def _merged_usage(
    entries: list[dict[str, int | str]], current: dict[str, int | str]
) -> list[dict[str, int | str]]:
    by_date = {str(entry["date"]): dict(entry) for entry in entries}
    day = str(current["date"])
    previous = by_date.get(day)
    if previous is not None:
        current = {
            "date": day,
            **{
                field: int(previous[field]) + int(current[field])
                for field in USAGE_NUMERIC_FIELDS
            },
        }
    by_date[day] = current
    return [by_date[key] for key in sorted(by_date)]


class _SafeFragmentParser(HTMLParser):
    _CONTAINERS = {"section", "h2", "h3", "p", "ul", "ol", "li", "strong", "em"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.valid = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not attrs:
            self.parts.append("<br>")
        elif tag in self._CONTAINERS and not attrs:
            self.stack.append(tag)
            self.parts.append(f"<{tag}>")
        else:
            self.valid = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not attrs:
            self.parts.append("<br>")
        else:
            self.valid = False

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.valid = False
            return
        self.stack.pop()
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(escape(data))

    def handle_comment(self, data: str) -> None:
        self.valid = False

    def handle_decl(self, decl: str) -> None:
        self.valid = False

    def unknown_decl(self, data: str) -> None:
        self.valid = False


def _safe_fragment(raw: object) -> str | None:
    if type(raw) is not dict or set(raw) != {"html"} or type(raw["html"]) is not str:
        return None
    parser = _SafeFragmentParser()
    try:
        parser.feed(raw["html"])
        parser.close()
    except Exception:
        return None
    if not parser.valid or parser.stack:
        return None
    rendered = "".join(parser.parts).strip()
    return rendered or None


def _digest_messages(records: list[PaperRecord]) -> list[dict[str, object]]:
    selected = [
        {
            "title": record.title,
            "authors": list(record.authors),
            "journal": record.journal,
            "published_at": record.published_at.astimezone(timezone.utc).isoformat(),
            "doi": record.doi,
            "url": record.url,
            "category": record.categories[0],
            "summary_zh": record.summary_zh,
        }
        for record in records
    ]
    return [
        {
            "role": "system",
            "content": (
                "Return one JSON object with exactly an html field containing a concise "
                "Chinese overview. Use only section, h2, h3, p, ul, ol, li, strong, em, and br "
                "tags without attributes."
            ),
        },
        {"role": "user", "content": json.dumps({"selected": selected}, ensure_ascii=False)},
    ]


def _safe_http_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and bool(parsed.netloc) else None


def _record_category(record: PaperRecord) -> str:
    return record.categories[0] if record.categories and record.categories[0] in CATEGORIES else "other-diamond"


def _deterministic_overview(count: int) -> str:
    return f"<section><h2>今日概览</h2><p>本期精选 {count} 篇金刚石相关论文。</p></section>"


def _render_html(
    records: list[PaperRecord], publication_title: str, day: str, overview: str | None
) -> str:
    groups: dict[str, list[PaperRecord]] = {}
    for record in records:
        groups.setdefault(_record_category(record), []).append(record)

    body = [overview or _deterministic_overview(len(records))]
    for category in sorted(groups):
        body.append(
            f'<section class="paper-group"><h2>{escape(CATEGORY_TITLES[category])}</h2>'
        )
        for record in sorted(
            groups[category], key=lambda item: (-item.published_at.timestamp(), item.title, item.url)
        ):
            title = escape(record.title)
            safe_url = _safe_http_url(record.url)
            heading = title if safe_url is None else f'<a href="{escape(safe_url, quote=True)}">{title}</a>'
            authors = escape("、".join(record.authors))
            journal = escape(record.journal)
            published = escape(record.published_at.astimezone(timezone.utc).date().isoformat())
            summary = escape(record.summary_zh or "")
            body.append(
                "<article>"
                f"<h3>{heading}</h3>"
                f'<p class="metadata">{authors} · {journal} · {published}</p>'
                f"<p>{summary}</p>"
                "</article>"
            )
        body.append("</section>")

    title = f"{publication_title} · 中文摘要"
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:auto;padding:2rem;line-height:1.65}"
        "article{border-top:1px solid #ddd;padding:1rem 0}.metadata{color:#555}a{color:#174ea6}</style>"
        "</head><body>"
        f"<header><h1>{escape(title)}</h1><p>{escape(day)}</p></header>"
        f"{''.join(body)}"
        "</body></html>\n"
    )


def _apply_decision(state: FeedState, decision: AiDecision) -> None:
    record = state.papers[decision.key]
    record.ai_relevant = decision.relevant
    record.ai_confidence = decision.confidence
    record.categories = [decision.category]
    record.summary_zh = decision.summary_zh


def _summary_stats(
    *,
    candidates: int,
    processed: int,
    selected: int,
    requests: int,
    remaining: int,
    failed: bool,
    token_before: tuple[int, int],
    token_after: tuple[int, int],
) -> SummaryStats:
    prompt = max(0, token_after[0] - token_before[0])
    completion = max(0, token_after[1] - token_before[1])
    return SummaryStats(
        candidates=candidates,
        processed=processed,
        selected=selected,
        requests=requests,
        remaining=remaining,
        failed=failed,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def run_summary(
    config: AppConfig,
    state_path: Path,
    client: JsonClient,
    now: datetime,
    *,
    output_dir: Path = Path("."),
) -> SummaryStats:
    """Process one bounded oldest-first queue slice and atomically publish its digest."""
    state_path = Path(state_path)
    output_dir = Path(output_dir)
    state = load_state(state_path)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    day = now.astimezone(timezone.utc).date().isoformat()
    rss_path = output_dir / RSS_NAME
    html_path = output_dir / HTML_NAME
    usage_path = output_dir / USAGE_NAME
    destinations = (rss_path, html_path, usage_path, state_path)
    validate_output_layout(destinations)
    previous_usage = _usage_entries(usage_path)

    candidate_keys = sorted(
        state.pending_ai, key=lambda key: state.papers[key].published_at
    )[: config.ai.daily_candidates]
    if not candidate_keys:
        return SummaryStats(0, 0, 0, 0, len(state.pending_ai), False)

    budget = RequestBudget(config.ai.max_requests)
    token_before = _client_token_totals(client)
    decisions: list[AiDecision] = []
    screening_failed = False
    for offset in range(0, len(candidate_keys), config.ai.batch_size):
        if budget.remaining <= 1:
            screening_failed = True
            break
        keys = candidate_keys[offset : offset + config.ai.batch_size]
        try:
            batch = screen_batch([state.papers[key] for key in keys], client, config.ai, budget)
        except (RuntimeError, ValueError):
            screening_failed = True
            break
        by_key = {decision.key: decision for decision in batch}
        decisions.extend(by_key[key] for key in keys)

    token_after_screening = _client_token_totals(client)
    if not decisions:
        return _summary_stats(
            candidates=len(candidate_keys),
            processed=0,
            selected=0,
            requests=budget.used,
            remaining=len(state.pending_ai),
            failed=True,
            token_before=token_before,
            token_after=token_after_screening,
        )

    processed_keys = {decision.key for decision in decisions}
    for decision in decisions:
        _apply_decision(state, decision)
    state.pending_ai = [key for key in state.pending_ai if key not in processed_keys]
    selected = [state.papers[decision.key] for decision in decisions if decision.relevant]

    overview = None
    screening_complete = not screening_failed and len(decisions) == len(candidate_keys)
    if selected and screening_complete and budget.remaining >= 1:
        try:
            raw_digest = client.complete_json(
                _digest_messages(selected), config.ai.digest_max_tokens, budget
            )
        except (RuntimeError, ValueError):
            raw_digest = None
        overview = _safe_fragment(raw_digest)

    token_after = _client_token_totals(client)
    stats = _summary_stats(
        candidates=len(candidate_keys),
        processed=len(decisions),
        selected=len(selected),
        requests=budget.used,
        remaining=len(state.pending_ai),
        failed=screening_failed or len(decisions) < len(candidate_keys),
        token_before=token_before,
        token_after=token_after,
    )
    feed_records = [replace(record, abstract=record.summary_zh or "") for record in selected]
    rss = render_rss(
        feed_records,
        f"{config.publication.title} · 中文摘要",
        config.publication.base_url,
        len(feed_records),
    )
    html = _render_html(selected, config.publication.title, day, overview)
    usage = json.dumps(
        _merged_usage(previous_usage, stats.usage_entry(day)),
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    staged: list[StagedFile] = []
    try:
        staged.append(stage_text(rss_path, rss))
        staged.append(stage_text(html_path, html))
        staged.append(stage_text(usage_path, usage))
        staged.append(stage_state(state_path, state))
    except Exception as staging_error:
        raise_with_cleanup(staging_error, "stage AI summary outputs", discard_staged(staged))
    try:
        commit_staged(staged)
    except Exception as commit_error:
        raise_with_cleanup(commit_error, "publish AI summary outputs", discard_staged(staged))
    return stats


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the daily Chinese diamond digest.")
    parser.add_argument("--config", type=Path, default=Path("paper_feed_config.json"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("DEEPSEEK_API_KEY is required", file=sys.stderr)
        return 2
    config = load_config(args.config)
    client = DeepSeekClient(key, config.ai)
    stats = run_summary(
        config,
        args.state,
        client,
        now or datetime.now(timezone.utc),
        output_dir=args.output_dir,
    )
    print(
        f"processed={stats.processed} selected={stats.selected} "
        f"requests={stats.requests} remaining={stats.remaining}"
    )
    return 0 if not stats.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
