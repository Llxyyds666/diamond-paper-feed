from pathlib import Path


WORKFLOW_DIRECTORY = Path(".github/workflows")


def _workflow_step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    assert marker in workflow
    return workflow.split(marker, 1)[1].split("\n      - name: ", 1)[0]


def test_github_workflows_obey_the_automation_contract():
    collect = (WORKFLOW_DIRECTORY / "collect.yml").read_text(encoding="utf-8")
    summarize = (WORKFLOW_DIRECTORY / "summarize.yml").read_text(encoding="utf-8")
    evaluate = (WORKFLOW_DIRECTORY / "evaluate.yml").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY" not in collect
    assert "SEMANTIC_SCHOLAR_API_KEY" not in collect
    assert "BARK_TOKEN" not in collect
    assert "python -m diamond_feed.enrich" not in collect
    assert "python -m diamond_feed.recommend" not in collect
    assert "python -m diamond_feed.notify" not in collect

    for command_or_secret in (
        "SEMANTIC_SCHOLAR_API_KEY",
        "BARK_TOKEN",
        "python -m diamond_feed.enrich",
        "python -m diamond_feed.recommend",
        "python -m diamond_feed.notify",
        "python -m diamond_feed.promote",
    ):
        assert command_or_secret not in evaluate

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
            "command": "python -m diamond_feed.summarize",
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
          SEMANTIC_SCHOLAR_API_KEY: ${{ secrets.SEMANTIC_SCHOLAR_API_KEY }}
          BARK_ENABLED: ${{ secrets.BARK_TOKEN != '' }}
        run: >-
          python -m diamond_feed.summarize
          --config paper_feed_config.json
          --state state.json
          --notification-plan "${{ runner.temp }}/diamond-notification.json"
"""
    assert expected_digest_step in summarize
    assert """      - name: Commit and push summary outputs
        id: publish
        if: ${{ always() && (steps.summarize.outcome == 'success' || steps.summarize.outcome == 'failure') }}
""" in summarize
    assert """      - name: Send Bark notifications
        if: ${{ inputs.smoke_test != true && steps.summarize.outcome == 'success' && steps.publish.outputs.pushed == 'true' }}
        continue-on-error: true
        env:
          BARK_TOKEN: ${{ secrets.BARK_TOKEN }}
        run: >-
          python -m diamond_feed.notify
          --plan "${{ runner.temp }}/diamond-notification.json"
""" in summarize
    assert """      - name: Propagate summary failure
        if: ${{ always() && steps.summarize.outcome == 'failure' }}
        run: exit 1
""" in summarize

    smoke_step = _workflow_step(summarize, "Run one logical DeepSeek smoke test")
    summary_step = _workflow_step(summarize, "Generate bounded DeepSeek digest")
    publish_step = _workflow_step(summarize, "Commit and push summary outputs")
    notify_step = _workflow_step(summarize, "Send Bark notifications")

    assert "SEMANTIC_SCHOLAR_API_KEY: ${{ secrets.SEMANTIC_SCHOLAR_API_KEY }}" in summary_step
    assert "BARK_ENABLED: ${{ secrets.BARK_TOKEN != '' }}" in summary_step
    assert "\n          BARK_TOKEN:" not in summary_step
    assert "SEMANTIC_SCHOLAR_API_KEY" not in smoke_step
    assert "BARK_TOKEN" not in smoke_step
    assert "RequestCounter()" in smoke_step
    assert "RequestBudget" not in smoke_step
    assert "id: publish" in publish_step
    assert 'echo "pushed=false" >> "$GITHUB_OUTPUT"' in publish_step
    assert 'git push origin "HEAD:${GITHUB_REF_NAME}"' in publish_step
    assert "steps.summarize.outcome == 'success'" in notify_step
    assert "steps.publish.outputs.pushed == 'true'" in notify_step
    assert "steps.publish.outcome" not in notify_step
    assert "continue-on-error: true" in notify_step
    assert "BARK_TOKEN: ${{ secrets.BARK_TOKEN }}" in notify_step
    assert "SEMANTIC_SCHOLAR_API_KEY" not in notify_step
    assert "python -m diamond_feed.notify" in notify_step
    assert summarize.index("git push origin") < summarize.index("python -m diamond_feed.notify")


def test_summary_notify_requires_an_actual_push_and_stages_only_summary_outputs():
    summarize = (WORKFLOW_DIRECTORY / "summarize.yml").read_text(encoding="utf-8")
    publish_step = _workflow_step(summarize, "Commit and push summary outputs")
    notify_step = _workflow_step(summarize, "Send Bark notifications")

    exact_allowlist = """          allowed_outputs=(
            "ai_summary_feed.xml"
            "ai_summary.html"
            "device_focus_feed.xml"
            "ai_usage.json"
            "state.json"
          )
"""
    assert exact_allowlist in publish_step
    assert "diamond-notification.json" not in publish_step
    assert "runner.temp" not in publish_step
    assert "git add -- ." not in publish_step
    assert "git add -A" not in publish_step

    pushed_false = 'echo "pushed=false" >> "$GITHUB_OUTPUT"'
    no_op_guard = "if git diff --cached --quiet; then"
    push = 'git push origin "HEAD:${GITHUB_REF_NAME}"'
    pushed_true = 'echo "pushed=true" >> "$GITHUB_OUTPUT"'
    no_op_branch = publish_step.split(no_op_guard, 1)[1].split("fi", 1)[0]

    assert publish_step.index(pushed_false) < publish_step.index(no_op_guard)
    assert "exit 0" in no_op_branch
    assert publish_step.count(pushed_true) == 1
    assert publish_step.index(push) < publish_step.index(pushed_true)
    assert pushed_true not in publish_step.split(push, 1)[0]
    assert "steps.publish.outputs.pushed == 'true'" in notify_step


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
