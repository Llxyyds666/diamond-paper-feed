from pathlib import Path


WORKFLOW_DIRECTORY = Path(".github/workflows")


def test_github_workflows_obey_the_automation_contract():
    collect = (WORKFLOW_DIRECTORY / "collect.yml").read_text(encoding="utf-8")
    summarize = (WORKFLOW_DIRECTORY / "summarize.yml").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY" not in collect
    assert summarize.count("DEEPSEEK_API_KEY") == 2
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in summarize

    expected = {
        "collect": {
            "text": collect,
            "cron": "0 */6 * * *",
            "command": (
                "python -m diamond_feed.collect --config paper_feed_config.json "
                "--state state.json"
            ),
            "staged": "git add -- filtered_feed.xml state.json fetch_failures.tsv",
        },
        "summarize": {
            "text": summarize,
            "cron": "0 0 * * *",
            "command": (
                "python -m diamond_feed.summarize --config paper_feed_config.json "
                "--state state.json"
            ),
            "staged": (
                "git add -- ai_summary_feed.xml ai_summary.html ai_usage.json state.json"
            ),
        },
    }

    for contract in expected.values():
        text = contract["text"]
        assert f"cron: '{contract['cron']}'" in text
        assert "contents: write" in text
        assert "group: diamond-paper-feed-${{ github.ref }}" in text
        assert "cancel-in-progress: false" in text
        assert "uses: actions/checkout@v6" in text
        assert "fetch-depth: 0" in text
        assert "uses: actions/setup-python@v6" in text
        assert "python-version: '3.11'" in text
        assert 'python -m pip install ".[dev]"' in text
        assert "python -m pytest -q" in text
        assert contract["command"] in text
        assert contract["staged"] in text
        assert "if git diff --cached --quiet; then" in text
        assert 'git pull --rebase origin "${GITHUB_REF_NAME}"' in text
        assert text.index("python -m pytest -q") < text.index(contract["command"])
        assert text.index(contract["staged"]) < text.index("git commit -m")
        assert text.index("git commit -m") < text.index("git pull --rebase")
        assert text.index("git pull --rebase") < text.index("git push")

    expected_digest_step = """      - name: Generate bounded DeepSeek digest
        env:
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
        run: python -m diamond_feed.summarize --config paper_feed_config.json --state state.json
"""
    assert expected_digest_step in summarize
