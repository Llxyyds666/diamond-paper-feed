from pathlib import Path

import pytest

from diamond_feed.config import load_config


def test_repository_config_has_locked_safety_limits():
    config = load_config(Path("paper_feed_config.json"))
    assert config.collection.lookback_days == 30
    assert config.collection.raw_feed_max_items == 2000
    assert config.ai.model == "deepseek-v4-flash-vision-exp"
    assert config.ai.daily_candidates == 40
    assert config.ai.batch_size == 10
    assert config.ai.max_requests == 5
    assert config.ai.max_abstract_chars == 1200


def test_invalid_batch_budget_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"collection":{"lookback_days":30,"raw_feed_max_items":2000,'
        '"http_timeout_seconds":30,"http_attempts":3},'
        '"ai":{"base_url":"https://api.deepseek.com","model":"deepseek-v4-flash-vision-exp",'
        '"daily_candidates":40,"batch_size":10,"max_requests":4,"max_abstract_chars":1200,'
        '"screening_max_tokens":4096,"digest_max_tokens":8192},'
        '"publication":{"title":"Diamond Paper Feed","base_url":""}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reserve one request"):
        load_config(path)
