"""Reversibly archive the November 15 indexed-date backfill regression.

This is an explicit maintenance operation, not a rolling retention policy.
Normal cumulative history is protected by its pre-regression identities.
"""

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from diamond_feed.atomic import commit_staged, discard_staged, stage_text
from diamond_feed.config import load_config
from diamond_feed.focus import FOCUS_RSS_NAME, load_focus_overrides, render_focused_rss
from diamond_feed.normalize import identity_aliases
from diamond_feed.publication import load_withheld_aliases
from diamond_feed.recency import prune_stale_pending
from diamond_feed.render import render_rss
from diamond_feed.state import FeedState, _as_state, _payload, load_state, stage_state
from diamond_feed.summarize import HTML_NAME, RSS_NAME, render_publication


def recovery_plan(
    state: FeedState, baseline: FeedState, cutoff: date,
) -> tuple[FeedState, FeedState]:
    """Plan archival of newly imported old papers; do not modify either input."""
    protected = {alias for paper in baseline.papers.values() for alias in identity_aliases(paper)}
    stale = {
        key for key, paper in state.papers.items()
        if paper.published_at.astimezone(timezone.utc).date() < cutoff
        and not protected.intersection(identity_aliases(paper))
    }
    cleaned = replace(
        state,
        papers={key: paper for key, paper in state.papers.items() if key not in stale},
        pending_ai=[key for key in state.pending_ai if key not in stale],
        source_watermarks=dict(state.source_watermarks),
        source_continuations=dict(state.source_continuations),
    )
    # Crossref cursors belong to the old query, which had no publication filter.
    old_cursor = cleaned.source_continuations.pop("crossref", None)
    cleaned.source_watermarks.pop("crossref", None)
    archived = FeedState(
        papers={key: paper for key, paper in state.papers.items() if key in stale},
        pending_ai=[key for key in state.pending_ai if key in stale],
        source_continuations={"crossref": old_cursor} if old_cursor is not None else {},
    )
    return cleaned, archived


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Pre-regression Git commit SHA")
    parser.add_argument("--cutoff", type=date.fromisoformat, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--config", type=Path, default=Path("paper_feed_config.json"))
    parser.add_argument("--now", type=datetime.fromisoformat)
    parser.add_argument("--apply", action="store_true", help="Otherwise only print the recovery plan")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.baseline):
        parser.error("baseline must be an immutable commit SHA")
    baseline_sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.baseline + "^{commit}"], text=True
    ).strip()
    snapshot_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    baseline = _as_state(json.loads(subprocess.check_output(["git", "show", baseline_sha + ":state.json"])))
    state = load_state(args.state)
    cleaned, archived = recovery_plan(state, baseline, args.cutoff)
    config = load_config(args.config)
    now = args.now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        parser.error("now must include a timezone")
    pruned = prune_stale_pending(cleaned, now, config.collection.lookback_days)
    print(json.dumps({
        "archived": len(archived.papers), "archived_pending": len(archived.pending_ai),
        "archived_selected": sum(p.ai_relevant is True for p in archived.papers.values()),
        "preserved": len(cleaned.papers), "remaining_recent_pending": len(cleaned.pending_ai),
        "additional_stale_pending_pruned": pruned, "apply": args.apply,
    }))
    if not args.apply:
        return 0
    if args.archive.exists():
        parser.error("archive already exists; refusing to overwrite recovery evidence")
    root = args.state.parent
    withheld = load_withheld_aliases(root / "config/ai_publication.json")
    overrides = load_focus_overrides(root / "config/device_focus_overrides.json")
    rss, html = render_publication(cleaned, config, now.astimezone(timezone.utc).date().isoformat(),
                                   None, withheld, new_count=0)
    archive_payload = {
        "version": 1, "reason": "Old publications recently reindexed were treated as new papers",
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "baseline_commit": baseline_sha, "snapshot_commit": snapshot_sha,
        "publication_cutoff": args.cutoff.isoformat(),
        # Includes all original queues/watermarks/cursors, even for protected
        # history. Each full record is stored once; archived_keys marks removals.
        "original_state": _payload(state), "archived_keys": sorted(archived.papers),
    }
    outputs = {
        args.archive: json.dumps(archive_payload, ensure_ascii=False, indent=2) + "\n",
        root / "filtered_feed.xml": render_rss(list(cleaned.papers.values()), config.publication.title,
                                               config.publication.base_url, config.collection.raw_feed_max_items),
        root / RSS_NAME: rss, root / HTML_NAME: html,
        root / FOCUS_RSS_NAME: render_focused_rss(cleaned, config, withheld, overrides),
    }
    staged = []
    try:
        for destination, content in outputs.items():
            staged.append(stage_text(destination, content))
        staged.append(stage_state(args.state, cleaned))
        commit_staged(staged)
    except Exception:
        discard_staged(staged)
        raise
    # ai_usage and ncd_watch deliberately remain unchanged: costs and accepted
    # delivery receipts are real audit history, not successful current content.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
