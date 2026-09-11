from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.error import HTTPError

import pytest

from diamond_feed import summarize
from diamond_feed import evaluate
from diamond_feed.evaluate import balance_change, read_balance, run_evaluation


def test_full_evaluation_preserves_production_queue_and_usage(
    tmp_path, configured_state_with_100_pending, app_config, fake_deepseek_client,
    monkeypatch,
):
    monkeypatch.setattr(
        summarize,
        "AbstractEnricher",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("evaluation created enrichment service")
        ),
    )
    state_before = configured_state_with_100_pending.read_bytes()
    production_usage = tmp_path / "ai_usage.json"
    production_usage.write_text('[{"existing":"ledger"}]', encoding="utf-8")
    output = tmp_path / "evaluations" / "one"

    report = run_evaluation(
        app_config, configured_state_with_100_pending, fake_deepseek_client,
        datetime(2026, 9, 8, tzinfo=timezone.utc), output_dir=output,
    )

    assert report["stats"]["processed"] == 40
    assert report["stats"]["requests"] == 5
    assert report["stats"]["failed"] is False
    assert len(report["papers"]) == 40
    assert all(p["ai_relevant"] is True for p in report["papers"])
    assert (output / "ai_summary.html").exists()
    assert configured_state_with_100_pending.read_bytes() == state_before
    assert production_usage.read_text(encoding="utf-8") == '[{"existing":"ledger"}]'
    assert not (output / "state.json").exists()


def test_evaluation_refuses_reusing_a_run_directory_before_network(
    tmp_path, configured_state_with_100_pending, app_config, fake_deepseek_client
):
    with pytest.raises(FileExistsError):
        run_evaluation(
            app_config, configured_state_with_100_pending, fake_deepseek_client,
            datetime(2026, 9, 8, tzinfo=timezone.utc), output_dir=tmp_path,
        )
    assert fake_deepseek_client.requests == 0


def test_failed_evaluation_keeps_diagnostics_and_usage(
    tmp_path, configured_state_with_100_pending, app_config, failing_deepseek_client
):
    output = tmp_path / "evaluation"
    report = run_evaluation(
        app_config, configured_state_with_100_pending, failing_deepseek_client,
        datetime(2026, 9, 8, tzinfo=timezone.utc), output_dir=output,
    )
    assert report["stats"]["failed"] is True
    assert report["stats"]["requests"] == 1
    assert json.loads((output / "ai_usage.json").read_text(encoding="utf-8"))[0]["requests"] == 1


def test_balance_report_discloses_only_change_not_private_balances():
    delta = balance_change({"CNY": Decimal("15.999999")}, {"CNY": Decimal("15.940001")})
    assert delta == {"CNY": "0.059998"}
    assert balance_change(None, {"CNY": Decimal("15.94")}) is None
    assert balance_change({"CNY": Decimal("15.94")}, {"CNY": Decimal("16.00")}) is None


def test_balance_request_retries_transient_failures(monkeypatch):
    calls = []
    waits = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"balance_infos":[{"currency":"CNY","total_balance":"8.50"}]}'

    outcomes = iter([TimeoutError("temporary"), TimeoutError("temporary"), Response()])

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(evaluate, "urlopen", fake_urlopen)

    assert read_balance(
        "private-key", "https://api.deepseek.com", wait=waits.append
    ) == {"CNY": Decimal("8.50")}
    assert len(calls) == 3
    assert waits == [1.0, 2.0]


def test_balance_request_does_not_retry_permanent_401(monkeypatch):
    calls = []
    waits = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        raise HTTPError(request.full_url, 401, "private body", None, None)

    monkeypatch.setattr(evaluate, "urlopen", fake_urlopen)

    assert read_balance(
        "private-key", "https://api.deepseek.com", wait=waits.append
    ) is None
    assert len(calls) == 1
    assert waits == []
