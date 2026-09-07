import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree

from diamond_feed import collect
from diamond_feed.collect import ScholarlyHarvest
from diamond_feed.config import load_config
from diamond_feed.filtering import load_rules, matches_rules
from diamond_feed.models import PaperRecord
from diamond_feed.state import SourceContinuation, load_state
from diamond_feed.summarize import run_summary
from scripts.validate_rss_sources import DATABASE_ONLY_COVERAGE, SOFT_FAILURES


REQUIRED_README_SECTIONS = (
    "Overview",
    "Covered diamond categories",
    "Source architecture",
    "Local setup",
    "Configuration",
    "DeepSeek Secret setup",
    "Manual workflow runs",
    "GitHub Pages and Zotero URLs",
    "AI hard limits",
    "Failure report meanings",
    "Adding sources",
    "State recovery",
    "Security",
)
README_DATABASE_REASONS = {
    "ACS Applied Materials & Interfaces": "官方 RSS 返回 HTTP 403",
    "ACS Nano": "官方 RSS 返回 HTTP 403",
    "American Mineralogist": "未找到稳定的官方 RSS 地址",
    "Applied Physics Letters": "官方 RSS 已停用并返回 HTTP 404",
    "Carbon": "官方 RSS 未通过有界 GET 验证",
    "Carbon Trends": "官方 RSS 未通过有界 GET 验证",
    "Crystal Growth & Design": "官方 RSS 返回 HTTP 403",
    "Diamond and Related Materials": "官方 RSS 未通过有界 GET 验证",
    "Earth and Planetary Science Letters": "官方 RSS 未通过有界 GET 验证",
    "Journal of Applied Physics": "官方 RSS 已停用并返回 HTTP 404",
    "Journal of Carbon Research": "官方 RSS 返回 HTTP 404",
    "Journal of Physics D: Applied Physics": "官方 RSS 重定向到机器人验证页",
    "Nano Letters": "官方 RSS 返回 HTTP 403",
    "Physics of the Earth and Planetary Interiors": "官方 RSS 未通过有界 GET 验证",
    "Quantum Science and Technology": "尚无经验证的稳定官方 RSS 恢复路径",
    "Semiconductor Science and Technology": "官方 RSS 重定向到机器人验证页",
    "Surface and Coatings Technology": "官方 RSS 未通过有界 GET 验证",
}
EXPECTED_BASE_URL = "https://llxyyds666.github.io/diamond-paper-feed"


def _assert_repository_output_contract(root: Path) -> None:
    for name in ("filtered_feed.xml", "state.json", "fetch_failures.tsv"):
        assert (root / name).is_file(), f"required repository output is missing: {name}"

    state = load_state(root / "state.json")
    raw_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    config = json.loads((root / "paper_feed_config.json").read_text(encoding="utf-8"))
    rss_root = ElementTree.parse(root / "filtered_feed.xml").getroot()
    items = rss_root.findall("./channel/item")
    guids = [item.findtext("guid") for item in items]

    assert raw_state["version"] == 2
    assert rss_root.tag == "rss" and rss_root.attrib == {"version": "2.0"}
    assert rss_root.findtext("./channel/link") == EXPECTED_BASE_URL
    assert len(items) == min(len(state.papers), config["collection"]["raw_feed_max_items"])
    assert len(items) <= 2000
    assert None not in guids and len(guids) == len(set(guids))
    assert set(guids) <= set(state.papers)
    if len(state.papers) <= config["collection"]["raw_feed_max_items"]:
        assert set(guids) == set(state.papers)
    assert len(state.pending_ai) == len(set(state.pending_ai))
    assert set(state.pending_ai) <= set(state.papers)
    assert not (set(state.source_watermarks) & set(state.source_continuations))

    summary_rss = root / "ai_summary_feed.xml"
    summary_html = root / "ai_summary.html"
    usage_path = root / "ai_usage.json"
    assert summary_rss.exists() == summary_html.exists()
    if summary_rss.exists():
        assert usage_path.exists()
        ElementTree.parse(summary_rss)
        assert "<html" in summary_html.read_text(encoding="utf-8").casefold()
    if usage_path.exists():
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
        assert isinstance(usage, list) and usage
        assert all(entry["candidates"] <= config["ai"]["daily_candidates"] for entry in usage)
        assert all(entry["requests"] <= config["ai"]["max_requests"] for entry in usage)


def _tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True
    )
    return [
        Path(raw.decode("utf-8"))
        for raw in result.stdout.split(b"\0")
        if raw and Path(raw.decode("utf-8")).is_file()
    ]


def _registry_rows() -> list[dict[str, str]]:
    with Path("config/rss_sources.tsv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        assert tuple(reader.fieldnames or ()) == ("name", "category", "url")
        return list(reader)


def _failure_rows() -> list[dict[str, str]]:
    with Path("fetch_failures.tsv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        assert tuple(reader.fieldnames or ()) == ("timestamp", "category", "url", "detail")
        return list(reader)


def test_repository_outputs_have_sustainable_cross_file_invariants():
    _assert_repository_output_contract(Path("."))

    state = load_state(Path("state.json"))
    rules = load_rules(Path("config/queries.json"))
    for record in state.papers.values():
        assert matches_rules(record, rules), f"unfiltered paper persisted: {record.title}"


def test_source_progress_and_failure_taxonomy_are_consistent():
    state = load_state(Path("state.json"))
    failures = _failure_rows()
    allowed_named_failures = set(SOFT_FAILURES) | {"parse_error", "retired"}

    assert len({row["url"] for row in failures}) == len(failures)
    for row in failures:
        parsed = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None and parsed.utcoffset() is not None
        assert row["category"] in allowed_named_failures or re.fullmatch(
            r"http_[1-5]\d\d", row["category"]
        )
        assert row["url"].startswith("https://")
        assert row["detail"].strip()
        if row["category"] == "http_403":
            assert row["url"].startswith("https://pubs.acs.org/")

    assert not (set(state.source_watermarks) & set(state.source_continuations))
    assert set(state.source_watermarks) | set(state.source_continuations)
    for continuation in state.source_continuations.values():
        assert continuation.cursor not in {"", "*"}
        assert continuation.from_date.isoformat() == continuation.from_date.strftime("%Y-%m-%d")


def test_isolated_collection_continuation_completion_and_summary_lifecycle(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "paper_feed_config.json"
    queries_path = tmp_path / "queries.json"
    sources_path = tmp_path / "rss_sources.tsv"
    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "filtered_feed.xml"
    failures_path = tmp_path / "fetch_failures.tsv"
    config_path.write_text(Path("paper_feed_config.json").read_text(encoding="utf-8"), encoding="utf-8")
    queries_path.write_text(Path("config/queries.json").read_text(encoding="utf-8"), encoding="utf-8")
    sources_path.write_text(
        "name\tcategory\turl\nTest Journal\tMaterials\thttps://example.test/feed.xml\n",
        encoding="utf-8",
    )
    paper = PaperRecord(
        title="Diamond-coated device membrane",
        abstract="CVD diamond coating on a biomedical device.",
        authors=["A. Author"],
        journal="Test Journal",
        published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        doi="10.1000/lifecycle",
        url="https://doi.org/10.1000/lifecycle",
        sources=["rss"],
        source_ids=["rss:lifecycle"],
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(collect, "collect_rss", lambda url, fetcher: ([paper], None))
    openalex_calls = 0

    def deterministic_harvest(name, query, from_date, fetcher, rows, continuation):
        nonlocal openalex_calls
        if name != "openalex":
            return ScholarlyHarvest(name, [], None, None, True, 1)
        openalex_calls += 1
        if openalex_calls == 1:
            assert continuation is None
            return ScholarlyHarvest(
                name,
                [],
                None,
                SourceContinuation(date(2026, 8, 8), "next-page"),
                False,
                1,
            )
        assert continuation == SourceContinuation(date(2026, 8, 8), "next-page")
        return ScholarlyHarvest(name, [], None, None, True, 1)

    monkeypatch.setattr(collect, "_collect_scholarly", deterministic_harvest)
    args = [
        "--config", str(config_path),
        "--state", str(state_path),
        "--sources", str(sources_path),
        "--queries", str(queries_path),
        "--feed", str(feed_path),
        "--failures", str(failures_path),
    ]

    assert collect.main(args, now=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc)) == 0
    first_state = load_state(state_path)
    assert first_state.source_continuations["openalex"].cursor == "next-page"
    assert not any((tmp_path / name).exists() for name in (
        "ai_summary_feed.xml", "ai_summary.html", "ai_usage.json"
    ))
    _assert_repository_output_contract(tmp_path)

    assert collect.main(args, now=lambda: datetime(2026, 9, 7, 6, tzinfo=timezone.utc)) == 0
    completed_state = load_state(state_path)
    assert "openalex" not in completed_state.source_continuations
    assert "openalex" in completed_state.source_watermarks

    class DeterministicSummaryClient:
        def complete_json(self, messages, max_tokens, budget):
            budget.consume()
            payload = json.loads(messages[-1]["content"])
            if "papers" not in payload:
                return {"html": "<section><h2>离线生命周期摘要</h2></section>"}
            return [
                {
                    "key": item["key"],
                    "relevant": True,
                    "confidence": 0.9,
                    "category": "films-membranes",
                    "matched_topics": ["diamond"],
                    "summary_zh": "金刚石涂层器件研究。",
                    "reason": "研究对象为金刚石涂层。",
                }
                for item in payload["papers"]
            ]

    stats = run_summary(
        load_config(config_path),
        state_path,
        DeterministicSummaryClient(),
        datetime(2026, 9, 8, tzinfo=timezone.utc),
        output_dir=tmp_path,
    )
    assert (stats.processed, stats.requests, stats.remaining) == (1, 2, 0)
    _assert_repository_output_contract(tmp_path)


def test_registry_is_exactly_the_curated_sorted_https_journal_set():
    rows = _registry_rows()

    assert len(rows) == 158
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["url"].startswith("https://") for row in rows)
    assert rows == sorted(
        rows,
        key=lambda row: (row["name"].casefold(), row["category"].casefold(), row["url"]),
    )
    registry_text = Path("config/rss_sources.tsv").read_text(encoding="utf-8").casefold()
    assert "perovskite" not in registry_text
    assert "arxiv.org/api/query" not in registry_text


def test_public_config_readme_and_database_only_table_describe_exact_limits():
    config = json.loads(Path("paper_feed_config.json").read_text(encoding="utf-8"))
    readme = Path("README.md").read_text(encoding="utf-8")

    assert config["publication"]["base_url"] == EXPECTED_BASE_URL
    for section in REQUIRED_README_SECTIONS:
        assert f"## {section}" in readme
    for output in ("filtered_feed.xml", "ai_summary_feed.xml", "ai_summary.html"):
        assert f"{EXPECTED_BASE_URL}/{output}" in readme
    assert "每天最多只向 AI 提交 40 篇候选" in readme
    assert "每批最多 10 篇" in readme
    assert "每轮最多 5 次 API 请求，所有失败重试也计数" in readme
    assert "1200 个 Unicode 字符" in readme
    assert "筛选请求的输出上限为 4096 token" in readme
    assert "最终摘要请求的输出上限为 8192 token" in readme
    assert "`filtered_feed.xml`：规则筛选后的高召回 RSS，最多 2000 条" in readme
    assert "第一次运行会回溯数据库最近 30 天" in readme
    assert "数据库游标" in readme and "不推进该来源水位" in readme
    assert set(README_DATABASE_REASONS) == set(DATABASE_ONLY_COVERAGE)
    for journal, translated_reason in README_DATABASE_REASONS.items():
        assert f"| {journal} | {translated_reason} |" in readme


def test_workflow_crons_and_secret_boundary_are_exact():
    collect_workflow = Path(".github/workflows/collect.yml").read_text(encoding="utf-8")
    summary_workflow = Path(".github/workflows/summarize.yml").read_text(encoding="utf-8")

    assert "cron: '0 */6 * * *'" in collect_workflow
    assert "DEEPSEEK_API_KEY" not in collect_workflow
    assert "secrets." not in collect_workflow
    assert "python -m diamond_feed.summarize" not in collect_workflow
    assert "cron: '0 0 * * *'" in summary_workflow
    assert summary_workflow.count("DEEPSEEK_API_KEY") == 2
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in summary_workflow
    assert "python -m diamond_feed.collect" not in summary_workflow


def test_all_tracked_text_is_free_of_credentials_and_local_machine_paths():
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in _tracked_text_files()
    )
    secret_prefix = "s" + "k-"
    key_name = "DEEPSEEK" + "_API_KEY"
    credential_patterns = (
        re.compile(re.escape(secret_prefix) + r"[A-Za-z0-9_-]{10,}"),
        re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._-]{10,}"),
        re.compile(
            rf"(?i){key_name}\s*[=:]\s*[\"']?[A-Za-z0-9_-]{{10,}}"
        ),
    )

    assert not any(pattern.search(text) for pattern in credential_patterns)
    assert not re.search(r"(?i)[A-Z]:\\Users\\[^\\\s]+", text)
    assert "deepseek-v4-flash-vision-exp" in text
