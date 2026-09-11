"""Explicit, isolated full-day evaluation with per-request cost observations."""

import argparse
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from diamond_feed.ai import CATEGORIES, DECISION_FIELDS, DeepSeekClient
from diamond_feed.config import load_config
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import group_records, record_key
from diamond_feed.retry import (
    DEFAULT_RETRY_POLICY,
    call_with_retry,
    retryable_http_status,
)
from diamond_feed.state import FeedState, load_state, save_state
from diamond_feed.summarize import run_summary


EVALUATION_CANDIDATE_LIMIT = 40


class ObservedClient(DeepSeekClient):
    def __init__(self, key, config):
        self.calls = []
        self.decisions = {}
        self.phase = "screening"
        super().__init__(key, config, transport=self._observed_transport)

    def _observed_transport(self, url, headers, payload, timeout):
        call = {"phase": self.phase, "attempt": len(self.calls) + 1}
        self.calls.append(call)
        started = monotonic()
        try:
            response = super()._default_transport(url, headers, payload, timeout)
        except Exception as error:
            call["transport_error"] = type(error).__name__
            raise
        finally:
            call["seconds"] = round(monotonic() - started, 3)
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        call["usage"] = {
            name: usage[name]
            for name in ("prompt_tokens", "completion_tokens", "total_tokens",
                         "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
            if type(usage.get(name)) is int
        }
        choices = response.get("choices", []) if isinstance(response, dict) else []
        if choices and isinstance(choices[0], dict):
            finish = choices[0].get("finish_reason")
            call["finish_reason"] = finish if finish in {
                "stop", "length", "content_filter", "tool_calls", "insufficient_system_resource"
            } else "unknown"
        print(json.dumps(call), flush=True)
        return response

    def complete_json(self, messages, max_tokens, budget):
        request = json.loads(messages[-1]["content"])
        self.phase = "screening" if "papers" in request else "digest"
        result = super().complete_json(messages, max_tokens, budget)
        if self.phase == "screening":
            decisions = result.get("decisions") if isinstance(result, dict) else result
            diagnostic = {"response_type": type(result).__name__}
            if isinstance(decisions, list):
                self.decisions.update({d["key"]: d for d in decisions
                                       if isinstance(d, dict) and type(d.get("key")) is str})
                expected = {p["key"] for p in request["papers"]}
                returned = [d.get("key") for d in decisions if isinstance(d, dict)]
                diagnostic["decision_count"] = len(decisions)
                diagnostic["keys_match"] = (
                    all(type(k) is str for k in returned)
                    and len(set(returned)) == len(returned)
                    and set(returned) == expected
                )
                diagnostic["invalid_fields_at"] = [
                    i for i, d in enumerate(decisions)
                    if not isinstance(d, dict) or set(d) != DECISION_FIELDS
                    or type(d.get("relevant")) is not bool
                    or type(d.get("category")) is not str or d["category"] not in CATEGORIES
                    or type(d.get("confidence")) not in (int, float)
                    or not 0 <= d["confidence"] <= 1
                    or type(d.get("matched_topics")) is not list
                    or not all(type(t) is str for t in d["matched_topics"])
                    or type(d.get("summary_zh")) is not str
                    or type(d.get("reason")) is not str
                ]
            self.calls[-1]["validation"] = diagnostic
            print(json.dumps(diagnostic), flush=True)
        return result


def read_balance(
    key: str,
    base_url: str,
    *,
    wait: Callable[[float], None] = time.sleep,
):
    """Return private balances in memory; neither logs nor reports contain them."""
    request = Request(
        base_url.rstrip("/") + "/user/balance",
        headers={"Authorization": "Bearer " + key},
    )

    def operation():
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def should_retry(error: Exception) -> bool:
        if isinstance(error, HTTPError):
            return retryable_http_status(error.code)
        return isinstance(
            error,
            (
                TimeoutError,
                ConnectionError,
                URLError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ),
        )

    try:
        data = call_with_retry(
            operation,
            should_retry,
            policy=DEFAULT_RETRY_POLICY,
            wait=wait,
        )
        balances = {item["currency"]: Decimal(item["total_balance"])
                    for item in data["balance_infos"] if item["currency"] in {"CNY", "USD"}}
        return balances if balances and all(x.is_finite() for x in balances.values()) else None
    except Exception:
        return None


def balance_change(before, after):
    if not before or not after or set(before) != set(after):
        return None
    differences = {currency: before[currency] - after[currency] for currency in before}
    if any(value < 0 for value in differences.values()):
        return None
    return {currency: str(value) for currency, value in differences.items()}


def run_evaluation(config, state_path, client, now, *, output_dir, baseline_path=None):
    """Run the same daily pipeline against an isolated oldest-first sample."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    state = FeedState() if baseline_path is not None else load_state(Path(state_path))
    keys = sorted(
        state.pending_ai, key=lambda k: state.papers[k].published_at
    )[:EVALUATION_CANDIDATE_LIMIT]
    baseline = None
    if baseline_path is not None:
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        inputs = baseline["papers"]
        if not 0 < len(inputs) <= EVALUATION_CANDIDATE_LIMIT:
            raise ValueError("baseline must fit the bounded daily candidate limit")
        state = FeedState()
        for item in inputs:
            key = item["key"]
            if type(item.get("authors")) is not list or not all(type(a) is str for a in item["authors"]):
                raise ValueError("baseline must snapshot authors; use replay-inputs.json for legacy evaluation 34174147048")
            record = PaperRecord(
                title=item["title"], abstract=item["input_abstract"],
                authors=item["authors"],
                journal=item["journal"], published_at=datetime.fromisoformat(item["published_at"]),
                doi=item["doi"], url=item["url"], sources=["baseline"], source_ids=[key],
            )
            if record_key(record) != key or key in state.papers:
                raise ValueError("baseline keys must be unique and canonical")
            state.papers[key] = record
            state.pending_ai.append(key)
        keys = state.pending_ai
    groups = group_records([state.papers[key] for key in keys])
    canonical = {alias: key for key, (_, aliases) in groups.items() for alias in aliases}
    started = monotonic()
    with TemporaryDirectory(prefix="diamond-evaluation-") as temporary:
        sample_path = Path(temporary) / "state.json"
        save_state(sample_path, FeedState(papers={k: state.papers[k] for k in keys}, pending_ai=keys))
        stats = run_summary(config, sample_path, client, now, output_dir=output_dir)
        result = load_state(sample_path)
    papers = [
        {"key": key, "canonical_key": canonical[key], "title": p.title, "doi": p.doi, "url": p.url,
         "authors": p.authors,
         "published_at": p.published_at.isoformat(), "journal": p.journal,
         "input_abstract": p.abstract[:config.ai.max_abstract_chars],
         "ai_relevant": p.ai_relevant, "ai_confidence": p.ai_confidence,
         "categories": p.categories, "summary_zh": p.summary_zh,
         "reason": getattr(client, "decisions", {}).get(canonical[key], {}).get("reason"),
         "baseline_relevant": next((item["ai_relevant"] for item in baseline["papers"]
                                    if item["key"] == key), None) if baseline else None}
        for key in keys for p in [result.papers[key]]
    ]
    html_path = output_dir / "ai_summary.html"
    overview_generated = html_path.exists() and "<p>本期精选 " not in html_path.read_text(encoding="utf-8")
    return {
        "date": now.isoformat(), "model": config.ai.model,
        "sample": "fixed baseline replay" if baseline else "oldest pending candidates (historical backlog, not publication-date today)",
        "baseline": Path(baseline_path).as_posix() if baseline_path else None,
        "screening_policy": "diamond-material-v2",
        "input_records": len(keys), "unique_records": len(groups),
        "duplicates_removed": len(keys) - len(groups),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "stats": asdict(stats), "elapsed_seconds": round(monotonic() - started, 3),
        "model_overview_generated": overview_generated,
        "calls": getattr(client, "calls", []), "papers": papers,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is required")
    config = load_config(Path("paper_feed_config.json"))
    before = read_balance(key, config.ai.base_url)
    client = ObservedClient(key, config.ai)
    report = run_evaluation(config, Path("state.json"), client,
                            datetime.now(timezone.utc), output_dir=args.output_dir, baseline_path=args.baseline)
    after = read_balance(key, config.ai.base_url)
    report["balance_decrease"] = balance_change(before, after)
    report["balance_note"] = "Account-level difference; other API activity or billing delay may affect it."
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"papers", "calls"}}, ensure_ascii=False))
    return 1 if report["stats"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
