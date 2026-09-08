from pathlib import Path


WORKFLOW_DIRECTORY = Path(".github/workflows")


def test_github_workflows_obey_the_automation_contract():
    collect = (WORKFLOW_DIRECTORY / "collect.yml").read_text(encoding="utf-8")
    summarize = (WORKFLOW_DIRECTORY / "summarize.yml").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY" not in collect
    assert summarize.count("DEEPSEEK_API_KEY") == 5
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in summarize

    expected = {
        "collect": {
            "text": collect,
            "cron": "0 */6 * * *",
            "command": (
                "python -m diamond_feed.collect --config paper_feed_config.json "
                "--state state.json"
            ),
            "outputs": ("filtered_feed.xml", "state.json", "fetch_failures.tsv"),
        },
        "summarize": {
            "text": summarize,
            "cron": "0 0 * * *",
            "command": (
                "python -m diamond_feed.summarize --config paper_feed_config.json "
                "--state state.json"
            ),
            "outputs": (
                "ai_summary_feed.xml",
                "ai_summary.html",
                "device_focus_feed.xml",
                "ai_usage.json",
                "state.json",
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
        assert "ref: ${{ github.ref_name }}" in text
        assert "fetch-depth: 0" in text
        assert "uses: actions/setup-python@v6" in text
        assert "python-version: '3.11'" in text
        assert 'python -m pip install ".[dev]"' in text
        assert "python -m pytest -q" in text
        assert contract["command"] in text
        assert "allowed_outputs=(" in text
        assert "existing_outputs=()" in text
        assert 'for output in "${allowed_outputs[@]}"; do' in text
        assert '[[ -e "${output}" ]]' in text
        assert 'git ls-files --error-unmatch -- "${output}"' in text
        assert 'existing_outputs+=("${output}")' in text
        assert 'if (( ${#existing_outputs[@]} > 0 )); then' in text
        assert 'git add -- "${existing_outputs[@]}"' in text
        for output in contract["outputs"]:
            assert f'"{output}"' in text
        fixed_pathspec = "git add -- " + " ".join(contract["outputs"])
        assert fixed_pathspec not in text
        assert "if git diff --cached --quiet; then" in text
        assert 'git pull --rebase origin "${GITHUB_REF_NAME}"' in text
        assert text.index("python -m pytest -q") < text.index(contract["command"])
        assert text.index('git add -- "${existing_outputs[@]}"') < text.index(
            "git commit -m"
        )
        assert text.index("git commit -m") < text.index("git pull --rebase")
        assert text.index("git pull --rebase") < text.index("git push")

    expected_digest_step = """      - name: Generate bounded DeepSeek digest
        id: summarize
        if: ${{ inputs.smoke_test != true }}
        continue-on-error: true
        env:
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
        run: python -m diamond_feed.summarize --config paper_feed_config.json --state state.json
"""
    assert expected_digest_step in summarize
    assert """      - name: Commit and push summary outputs
        if: ${{ always() && (steps.summarize.outcome == 'success' || steps.summarize.outcome == 'failure') }}
""" in summarize
    assert """      - name: Propagate summary failure
        if: ${{ always() && steps.summarize.outcome == 'failure' }}
        run: exit 1
""" in summarize


def test_empty_summary_queue_can_publish_without_preexisting_output_files():
    summarize = (WORKFLOW_DIRECTORY / "summarize.yml").read_text(encoding="utf-8")

    guarded_add = """          if (( ${#existing_outputs[@]} > 0 )); then
            git add -- "${existing_outputs[@]}"
          fi
          if git diff --cached --quiet; then
"""
    assert guarded_add in summarize
    assert (
        "git add -- ai_summary_feed.xml ai_summary.html device_focus_feed.xml ai_usage.json state.json"
        not in summarize
    )
