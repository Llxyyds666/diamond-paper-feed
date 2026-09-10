from pathlib import Path
import json

import pytest

from diamond_feed.config import load_config


VALID_CONFIG = {
    "collection": {
        "lookback_days": 30,
        "raw_feed_max_items": 2000,
        "http_timeout_seconds": 30,
        "http_attempts": 3,
    },
    "ai": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash-vision-exp",
        "daily_candidates": 40,
        "batch_size": 10,
        "max_requests": 5,
        "max_abstract_chars": 1200,
        "screening_max_tokens": 4096,
        "digest_max_tokens": 8192,
        "recommendation_max_tokens": 512,
    },
    "publication": {"title": "Diamond Paper Feed", "base_url": ""},
}


def write_config(path, config):
    path.write_text(json.dumps(config), encoding="utf-8")


def test_repository_config_has_locked_safety_limits():
    config = load_config(Path("paper_feed_config.json"))
    assert config.collection.lookback_days == 30
    assert config.collection.raw_feed_max_items == 2000
    assert config.ai.model == "deepseek-v4-flash-vision-exp"
    assert config.ai.daily_candidates == 40
    assert config.ai.batch_size == 10
    assert config.ai.max_requests == 6
    assert config.ai.max_abstract_chars == 1200
    assert config.ai.recommendation_max_tokens == 512


def test_invalid_batch_budget_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"collection":{"lookback_days":30,"raw_feed_max_items":2000,'
        '"http_timeout_seconds":30,"http_attempts":3},'
        '"ai":{"base_url":"https://api.deepseek.com","model":"deepseek-v4-flash-vision-exp",'
        '"daily_candidates":40,"batch_size":10,"max_requests":4,"max_abstract_chars":1200,'
        '"screening_max_tokens":4096,"digest_max_tokens":8192,'
        '"recommendation_max_tokens":512},'
        '"publication":{"title":"Diamond Paper Feed","base_url":""}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reserve one request"):
        load_config(path)


@pytest.mark.parametrize("section", ["root", "collection", "ai", "publication"])
def test_unknown_configuration_key_is_rejected(tmp_path, section):
    config = json.loads(json.dumps(VALID_CONFIG))
    target = config if section == "root" else config[section]
    target["unsupported"] = True
    path = tmp_path / "config.json"
    write_config(path, config)

    with pytest.raises(ValueError, match="unknown configuration key"):
        load_config(path)


@pytest.mark.parametrize(
    ("field", "excessive"),
    [
        ("daily_candidates", 41),
        ("batch_size", 11),
        ("max_requests", 7),
        ("max_abstract_chars", 1201),
        ("screening_max_tokens", 4097),
        ("digest_max_tokens", 8193),
        ("recommendation_max_tokens", 513),
    ],
)
def test_ai_limit_above_public_cap_is_rejected(tmp_path, field, excessive):
    config = json.loads(json.dumps(VALID_CONFIG))
    config["ai"][field] = excessive
    path = tmp_path / "config.json"
    write_config(path, config)

    with pytest.raises(ValueError, match=f"{field} must not exceed"):
        load_config(path)


def test_ai_limits_below_public_caps_are_accepted(tmp_path):
    config = json.loads(json.dumps(VALID_CONFIG))
    config["ai"].update(
        daily_candidates=20,
        batch_size=10,
        max_requests=3,
        max_abstract_chars=600,
        screening_max_tokens=2048,
        digest_max_tokens=4096,
        recommendation_max_tokens=256,
    )
    path = tmp_path / "config.json"
    write_config(path, config)

    assert load_config(path).ai.daily_candidates == 20


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("base_url", "https://example.invalid"),
        ("model", "another-model"),
    ],
)
def test_ai_target_must_match_pinned_public_default(tmp_path, field, invalid):
    config = json.loads(json.dumps(VALID_CONFIG))
    config["ai"][field] = invalid
    path = tmp_path / "config.json"
    write_config(path, config)

    with pytest.raises(ValueError, match=f"AI {field} must match"):
        load_config(path)
