import json

import pytest

from diamond_feed.notification import (
    build_notification_plan,
    load_notification_plan,
    write_notification_plan,
)
from diamond_feed.recommend import Recommendation


def test_valid_plan_contains_two_messages_and_tap_urls(tmp_path, diamond_records):
    record = diamond_records[0]
    record.categories = [
        "electronics-optoelectronics",
        "diamond-power-rf-detectors",
        "device-grade-single-crystal",
    ]
    plan = build_notification_plan(
        candidates=40,
        processed=40,
        selected=12,
        focus_selected=3,
        base_url="https://example.test/feed",
        recommendation=Recommendation(
            "doi:10.1000/diamond.1", "实验链路完整，适合入门。"
        ),
        recommendation_record=record,
        recommendation_failed=False,
    )
    path = tmp_path / "notification.json"
    write_notification_plan(path, plan)
    loaded = load_notification_plan(path)

    assert len(loaded.messages) == 2
    assert loaded.messages[0].title == "金刚石文献日报"
    assert loaded.messages[0].body == (
        "今日候选：40 篇\n"
        "完成筛选：40 篇\n"
        "综合入选：12 篇\n"
        "器件方向：3 篇"
    )
    assert loaded.messages[0].url == "https://example.test/feed/ai_summary.html"
    assert loaded.messages[1].title == "今日论文推荐"
    assert loaded.messages[1].body == (
        f"{record.title}\n"
        "方向：金刚石功率/射频/探测器件、金刚石单晶器件\n"
        "推荐理由：实验链路完整，适合入门。"
    )
    assert loaded.messages[1].url == record.url


def test_empty_focus_and_failed_recommendation_are_distinct():
    empty = build_notification_plan(
        candidates=10,
        processed=10,
        selected=2,
        focus_selected=0,
        base_url="https://example.test",
        recommendation=None,
        recommendation_record=None,
        recommendation_failed=False,
    )
    failed = build_notification_plan(
        candidates=10,
        processed=10,
        selected=2,
        focus_selected=1,
        base_url="https://example.test",
        recommendation=None,
        recommendation_record=None,
        recommendation_failed=True,
    )
    assert empty.messages[1].body == "今日无器件方向推荐"
    assert empty.messages[1].url is None
    assert failed.messages[1].body == "今日推荐生成失败，器件方向 RSS 已正常更新"
    assert failed.messages[1].url is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"version": 2, "messages": []},
        {"version": 1, "messages": [], "extra": True},
        {
            "version": 1,
            "messages": [
                {"title": "x", "body": "y", "url": "file:///x"},
                {"title": "x", "body": "y", "url": None},
            ],
        },
        {
            "version": 1,
            "messages": [
                {"title": "x", "body": "y", "url": None},
                {"title": "x", "body": "y", "url": None, "token": "secret"},
            ],
        },
    ],
)
def test_notification_plan_loader_rejects_invalid_schema(tmp_path, payload):
    path = tmp_path / "notification.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid notification plan"):
        load_notification_plan(path)


def test_notification_plan_loader_rejects_duplicate_json_fields(tmp_path):
    path = tmp_path / "notification.json"
    path.write_text(
        '{"version":1,"version":1,"messages":[]}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="invalid notification plan"):
        load_notification_plan(path)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/path\nforged",
        "https://example.test/path\rforged",
        "https://example.test/path\tforged",
        "https://example.test/path\x01forged",
        "https://example.test:99999/path",
    ],
)
def test_notification_plan_loader_rejects_control_characters_and_invalid_ports(
    tmp_path, url
):
    path = tmp_path / "notification.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "messages": [
                    {"title": "one", "body": "body", "url": url},
                    {"title": "two", "body": "body", "url": None},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid notification plan"):
        load_notification_plan(path)


def test_build_plan_rejects_mismatched_recommendation_record(diamond_records):
    with pytest.raises(ValueError, match="invalid notification plan"):
        build_notification_plan(
            candidates=1,
            processed=1,
            selected=1,
            focus_selected=1,
            base_url="https://example.test",
            recommendation=Recommendation("doi:not-the-record", "理由充分。"),
            recommendation_record=diamond_records[0],
            recommendation_failed=False,
        )
