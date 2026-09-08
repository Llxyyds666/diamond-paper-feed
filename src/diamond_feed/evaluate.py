"""Explicit, isolated full-day evaluation with per-request cost observations."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.request import Request, urlopen

from diamond_feed.ai import CATEGORIES, DECISION_FIELDS, DeepSeekClient
from diamond_feed.config import load_config
from diamond_feed.state import FeedState, load_state, save_state
from diamond_feed.summarize import run_summary


class ObservedClient(DeepSeekClient):
    def __init__(self, key, config):
        self.calls = []
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


def read_balance(key, base_url):
    """Return private balances in memory; neither logs nor reports contain them."""
    try:
        request = Request(base_url.rstrip("/") + "/user/balance",
                          headers={"Authorization": "Bearer " + key})
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
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


def run_evaluation(config, state_path, client, now, *, output_dir):
    """Run the same daily pipeline against an isolated oldest-first sample."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    state = load_state(Path(state_path))
    keys = sorted(state.pending_ai, key=lambda k: state.papers[k].published_at)[:config.ai.daily_candidates]
    started = monotonic()
    with TemporaryDirectory(prefix="diamond-evaluation-") as temporary:
        sample_path = Path(temporary) / "state.json"
        save_state(sample_path, FeedState(papers={k: state.papers[k] for k in keys}, pending_ai=keys))
        stats = run_summary(config, sample_path, client, now, output_dir=output_dir)
        result = load_state(sample_path)
    papers = [
        {"key": key, "title": p.title, "doi": p.doi, "url": p.url,
         "published_at": p.published_at.isoformat(), "journal": p.journal,
         "input_abstract": p.abstract[:config.ai.max_abstract_chars],
         "ai_relevant": p.ai_relevant, "ai_confidence": p.ai_confidence,
         "categories": p.categories, "summary_zh": p.summary_zh}
        for key in keys for p in [result.papers[key]]
    ]
    html_path = output_dir / "ai_summary.html"
    overview_generated = html_path.exists() and "<p>本期精选 " not in html_path.read_text(encoding="utf-8")
    return {
        "date": now.isoformat(), "model": config.ai.model,
        "sample": "oldest 40 pending candidates (historical backlog, not publication-date today)",
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "stats": asdict(stats), "elapsed_seconds": round(monotonic() - started, 3),
        "model_overview_generated": overview_generated,
        "calls": getattr(client, "calls", []), "papers": papers,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is required")
    config = load_config(Path("paper_feed_config.json"))
    before = read_balance(key, config.ai.base_url)
    client = ObservedClient(key, config.ai)
    report = run_evaluation(config, Path("state.json"), client,
                            datetime.now(timezone.utc), output_dir=args.output_dir)
    after = read_balance(key, config.ai.base_url)
    report["balance_decrease"] = balance_change(before, after)
    report["balance_note"] = "Account-level difference; other API activity or billing delay may affect it."
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"papers", "calls"}}, ensure_ascii=False))
    return 1 if report["stats"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
