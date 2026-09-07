import csv
import json
from pathlib import Path
from xml.etree import ElementTree

from diamond_feed.state import load_state
from scripts.validate_rss_sources import DATABASE_ONLY_COVERAGE


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


def _tracked_contract_text() -> str:
    roots = [Path("src"), Path("config"), Path(".github")]
    roots.extend([Path("README.md"), Path("paper_feed_config.json")])
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in files
        if path.stat().st_size < 2_000_000
    )


def test_repository_outputs_and_security_contract():
    for name in ("filtered_feed.xml", "ai_summary_feed.xml"):
        path = Path(name)
        if path.exists():
            ElementTree.parse(path)

    if Path("state.json").exists():
        load_state(Path("state.json"))

    failures_path = Path("fetch_failures.tsv")
    if failures_path.exists():
        with failures_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            assert tuple(reader.fieldnames or ()) == ("timestamp", "category", "url", "detail")
            list(reader)

    tracked_text = _tracked_contract_text()
    assert "DEEPSEEK_API_KEY" + "=" not in tracked_text
    assert "Authorization: Bearer " + "sk-" not in tracked_text
    assert "deepseek-v4-flash-vision-exp" in Path("paper_feed_config.json").read_text(encoding="utf-8")


def test_public_config_and_readme_describe_the_deployment_contract():
    config = json.loads(Path("paper_feed_config.json").read_text(encoding="utf-8"))
    assert config["publication"]["base_url"] == "https://llxyyds666.github.io/diamond-paper-feed"

    readme = Path("README.md").read_text(encoding="utf-8")
    for section in REQUIRED_README_SECTIONS:
        assert f"## {section}" in readme
    assert "https://llxyyds666.github.io/diamond-paper-feed/filtered_feed.xml" in readme
    assert "https://llxyyds666.github.io/diamond-paper-feed/ai_summary_feed.xml" in readme
    assert "https://llxyyds666.github.io/diamond-paper-feed/ai_summary.html" in readme
    assert "40" in readme
    assert "5" in readme
    assert "10" in readme
    assert "deepseek-v4-flash-vision-exp" in readme
    for journal in DATABASE_ONLY_COVERAGE:
        assert journal in readme
