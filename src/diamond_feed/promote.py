"""Promote a completed evaluation into the cumulative feed without using AI."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

from diamond_feed.ai import CATEGORIES
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
from diamond_feed.normalize import group_records, record_key
from diamond_feed.publication import cumulative_records, load_withheld_aliases
from diamond_feed.state import FeedState, load_state, stage_state
from diamond_feed.summarize import HTML_NAME, RSS_NAME, USAGE_NAME, _apply_decision, render_publication


REPORT_PAPER_FIELDS = {
    "key",
    "canonical_key",
    "title",
    "authors",
    "doi",
    "url",
    "journal",
    "published_at",
    "input_abstract",
    "ai_relevant",
    "ai_confidence",
    "categories",
    "summary_zh",
    "reason",
}


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"evaluation report has invalid {name}")
    return value


def _report_date(value: object) -> str:
    if type(value) is not str:
        raise ValueError("evaluation report has invalid date")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("evaluation report has invalid date") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("evaluation report date must include a timezone")
    return moment.astimezone(timezone.utc).date().isoformat()


def _decision(item: dict[str, object]) -> AiDecision:
    relevant = item["ai_relevant"]
    confidence = item["ai_confidence"]
    categories = item["categories"]
    summary = item["summary_zh"]
    reason = item["reason"]
    if type(relevant) is not bool:
        raise ValueError("evaluation report has invalid AI relevance")
    if (
        type(confidence) not in (int, float)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("evaluation report has invalid AI confidence")
    if (
        type(categories) is not list
        or len(categories) != 1
        or type(categories[0]) is not str
        or categories[0] not in CATEGORIES
    ):
        raise ValueError("evaluation report has invalid AI category")
    if type(summary) is not str or type(reason) is not str:
        raise ValueError("evaluation report has invalid AI text")
    if relevant and not summary.strip():
        raise ValueError("accepted evaluation paper requires a summary")
    return AiDecision(
        key=item["key"],  # type: ignore[arg-type]
        relevant=relevant,
        confidence=float(confidence),
        category=categories[0],
        matched_topics=[],
        summary_zh=summary,
        reason=reason,
    )


def _validate_metadata(item: dict[str, object], record: PaperRecord, config: AppConfig) -> None:
    expected = {
        "title": record.title,
        "authors": record.authors,
        "doi": record.doi,
        "url": record.url,
        "journal": record.journal,
        "published_at": record.to_dict()["published_at"],
        "input_abstract": record.abstract[: config.ai.max_abstract_chars],
    }
    if any(item[field] != value for field, value in expected.items()):
        raise ValueError("evaluation report metadata no longer matches state")


def _validated_report(
    payload: object, state: FeedState, config: AppConfig
) -> tuple[str, dict[str, AiDecision], dict[str, list[str]], int]:
    if type(payload) is not dict:
        raise ValueError("evaluation report must be an object")
    day = _report_date(payload.get("date"))
    papers = payload.get("papers")
    stats = payload.get("stats")
    if type(papers) is not list or not papers or type(stats) is not dict:
        raise ValueError("evaluation report is incomplete")
    if stats.get("failed") is not False:
        raise ValueError("evaluation report did not complete successfully")
    input_records = _integer(payload.get("input_records"), "input_records", minimum=1)
    unique_records = _integer(payload.get("unique_records"), "unique_records", minimum=1)
    if input_records != len(papers):
        raise ValueError("evaluation report paper count is incomplete")

    keys: list[str] = []
    items: dict[str, dict[str, object]] = {}
    for raw_item in papers:
        if type(raw_item) is not dict or not REPORT_PAPER_FIELDS <= set(raw_item):
            raise ValueError("evaluation report contains an invalid paper")
        key = raw_item["key"]
        canonical_key = raw_item["canonical_key"]
        if type(key) is not str or type(canonical_key) is not str or key not in state.papers:
            raise ValueError("evaluation report references an unknown paper")
        if key in items or record_key(state.papers[key]) != key:
            raise ValueError("evaluation report contains duplicate or stale paper keys")
        _validate_metadata(raw_item, state.papers[key], config)
        keys.append(key)
        items[key] = raw_item

    report_groups = group_records([state.papers[key] for key in keys])
    if unique_records != len(report_groups):
        raise ValueError("evaluation report unique paper count is inconsistent")
    if (
        _integer(stats.get("candidates"), "stats.candidates") != unique_records
        or _integer(stats.get("processed"), "stats.processed") != unique_records
    ):
        raise ValueError("evaluation report is only partially processed")

    decisions: dict[str, AiDecision] = {}
    members_by_canonical: dict[str, list[str]] = {}
    accepted_groups = 0
    for canonical_key, (_, member_keys) in report_groups.items():
        members_by_canonical[canonical_key] = member_keys
        group_decisions: list[AiDecision] = []
        for key in member_keys:
            item = items[key]
            if item["canonical_key"] != canonical_key:
                raise ValueError("evaluation report canonical identity is stale")
            decision = _decision(item)
            decisions[key] = decision
            group_decisions.append(decision)
        signatures = {
            (
                decision.relevant,
                decision.confidence,
                decision.category,
                decision.summary_zh,
                decision.reason,
            )
            for decision in group_decisions
        }
        if len(signatures) != 1:
            raise ValueError("evaluation report has conflicting alias decisions")
        accepted_groups += int(group_decisions[0].relevant)
    if _integer(stats.get("selected"), "stats.selected") != accepted_groups:
        raise ValueError("evaluation report selected count is inconsistent")
    return day, decisions, members_by_canonical, accepted_groups


def _load_report(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid evaluation report {path}") from error


def promote_evaluation(
    config: AppConfig,
    state_path: Path,
    report_path: Path,
    now: datetime,
    *,
    output_dir: Path = Path("."),
    policy_path: Path | None = None,
) -> dict[str, object]:
    """Validate and atomically publish saved decisions without a provider request."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    state_path = Path(state_path)
    report_path = Path(report_path)
    output_dir = Path(output_dir)
    policy_path = (
        Path(policy_path)
        if policy_path is not None
        else state_path.parent / "config" / "ai_publication.json"
    )
    rss_path = output_dir / RSS_NAME
    html_path = output_dir / HTML_NAME
    usage_path = output_dir / USAGE_NAME
    # Read-only inputs are included so that no input can alias a destination.
    validate_output_layout((rss_path, html_path, state_path, report_path, policy_path, usage_path))

    state = load_state(state_path)
    report_payload = _load_report(report_path)
    day, decisions, report_groups, accepted_groups = _validated_report(
        report_payload, state, config
    )
    withheld_aliases = load_withheld_aliases(policy_path)

    all_groups = group_records(list(state.papers.values()))
    group_by_member = {
        member: (canonical, members)
        for canonical, (_, members) in all_groups.items()
        for member in members
    }
    processed: set[str] = set()
    for report_canonical, report_members in report_groups.items():
        representative = decisions[report_members[0]]
        full_canonical, full_members = group_by_member[report_members[0]]
        if (
            full_canonical != report_canonical
            or any(group_by_member[key][0] != full_canonical for key in report_members)
        ):
            raise ValueError("evaluation canonical identity no longer matches state")
        for key in full_members:
            _apply_decision(state, replace(representative, key=key))
        processed.update(full_members)
    state.pending_ai = [key for key in state.pending_ai if key not in processed]

    published_records = cumulative_records(state, withheld_aliases)
    overview = "本页复用已完成评测的筛选与摘要；本次离线导入没有调用 AI。"
    rss, html = render_publication(
        state,
        config,
        day,
        overview,
        withheld_aliases,
        new_count=accepted_groups,
    )
    staged: list[StagedFile] = []
    try:
        staged.append(stage_text(rss_path, rss))
        staged.append(stage_text(html_path, html))
        staged.append(stage_state(state_path, state))
    except Exception as staging_error:
        raise_with_cleanup(
            staging_error,
            "stage offline evaluation promotion",
            discard_staged(staged),
        )
    try:
        commit_staged(staged)
    except Exception as commit_error:
        raise_with_cleanup(
            commit_error,
            "publish offline evaluation promotion",
            discard_staged(staged),
        )
    return {
        "run_id": report_payload.get("run_id"),  # type: ignore[union-attr]
        "imported": len(decisions),
        "selected": accepted_groups,
        "published": len(published_records),
        "remaining": len(state.pending_ai),
        "model_requests": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Promote a completed AI evaluation without making model requests."
    )
    parser.add_argument("--config", type=Path, default=Path("paper_feed_config.json"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args(argv)
    try:
        result = promote_evaluation(
            load_config(args.config),
            args.state,
            args.report,
            datetime.now(timezone.utc),
            output_dir=args.output_dir,
            policy_path=args.policy,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"offline promotion failed: {error}", file=sys.stderr)
        return 1
    print(
        f"imported={result['imported']} selected={result['selected']} "
        f"published={result['published']} requests=0 remaining={result['remaining']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
