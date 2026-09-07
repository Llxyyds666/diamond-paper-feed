import csv
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree

from diamond_feed.filtering import load_rules, matches_rules
from diamond_feed.state import load_state
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
TRANSIENT_HTTP_FAILURES = {"http_429", "http_500", "http_502", "http_503", "http_504"}
EXPECTED_BASE_URL = "https://llxyyds666.github.io/diamond-paper-feed"


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


def test_task10_outputs_have_strong_cross_file_invariants_and_no_ai_results():
    for name in ("filtered_feed.xml", "state.json", "fetch_failures.tsv"):
        assert Path(name).is_file(), f"required Task 10 output is missing: {name}"
    for name in ("ai_summary_feed.xml", "ai_summary.html", "ai_usage.json"):
        assert not Path(name).exists(), f"collection smoke must not create {name}"

    config = json.loads(Path("paper_feed_config.json").read_text(encoding="utf-8"))
    raw_state = json.loads(Path("state.json").read_text(encoding="utf-8"))
    state = load_state(Path("state.json"))
    root = ElementTree.parse("filtered_feed.xml").getroot()
    items = root.findall("./channel/item")
    guids = [item.findtext("guid") for item in items]

    assert raw_state["version"] == 2
    assert root.tag == "rss" and root.attrib == {"version": "2.0"}
    assert root.findtext("./channel/link") == EXPECTED_BASE_URL
    assert len(items) == min(len(state.papers), config["collection"]["raw_feed_max_items"])
    assert len(items) <= 2000
    assert None not in guids and len(guids) == len(set(guids))
    assert set(guids) <= set(state.papers)
    if len(state.papers) <= 2000:
        assert set(guids) == set(state.papers)
    assert len(state.pending_ai) == len(set(state.pending_ai))
    assert set(state.pending_ai) == set(state.papers)

    rules = load_rules(Path("config/queries.json"))
    for record in state.papers.values():
        assert record.ai_relevant is None
        assert record.ai_confidence is None
        assert record.summary_zh is None
        assert matches_rules(record, rules), f"unfiltered paper persisted: {record.title}"


def test_smoke_source_watermarks_continuation_and_failure_taxonomy_are_consistent():
    state = load_state(Path("state.json"))
    registry = _registry_rows()
    failures = _failure_rows()
    registry_urls = {row["url"] for row in registry}
    failed_rss_urls = {row["url"] for row in failures if row["url"] in registry_urls}
    allowed_failures = set(SOFT_FAILURES) | TRANSIENT_HTTP_FAILURES | {"http_403"}

    assert failures
    assert len({row["url"] for row in failures}) == len(failures)
    for row in failures:
        parsed = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None and parsed.utcoffset() is not None
        assert row["category"] in allowed_failures
        assert row["url"].startswith("https://")
        assert row["detail"].strip()
        if row["category"] == "http_403":
            assert row["url"].startswith("https://pubs.acs.org/")

    expected_watermarks = {
        f"rss:{url}" for url in registry_urls - failed_rss_urls
    } | {"crossref", "arxiv"}
    assert set(state.source_watermarks) == expected_watermarks
    assert set(state.source_continuations) == {"openalex"}
    assert "openalex" not in state.source_watermarks
    continuation = state.source_continuations["openalex"]
    assert continuation.cursor not in {"", "*"}
    assert continuation.from_date.isoformat() == continuation.from_date.strftime("%Y-%m-%d")
    assert not ({f"rss:{url}" for url in failed_rss_urls} & set(state.source_watermarks))


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
