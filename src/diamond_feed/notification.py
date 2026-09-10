"""Strict, secret-free notification plans for post-publication delivery."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from diamond_feed.atomic import atomic_write_text
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import record_key
from diamond_feed.recommend import Recommendation, focus_names


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    title: str
    body: str
    url: str | None


@dataclass(frozen=True, slots=True)
class NotificationPlan:
    messages: tuple[NotificationMessage, ...]


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _is_http_url(value: str) -> bool:
    if any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _validated_plan(raw: object) -> NotificationPlan:
    if type(raw) is not dict or set(raw) != {"version", "messages"}:
        raise ValueError("invalid notification plan")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("invalid notification plan")
    messages = raw["messages"]
    if type(messages) is not list or len(messages) != 2:
        raise ValueError("invalid notification plan")

    parsed: list[NotificationMessage] = []
    for message in messages:
        if type(message) is not dict or set(message) != {"title", "body", "url"}:
            raise ValueError("invalid notification plan")
        title, body, url = message["title"], message["body"], message["url"]
        if (
            type(title) is not str
            or not title.strip()
            or type(body) is not str
            or not body.strip()
            or (url is not None and (type(url) is not str or not _is_http_url(url)))
        ):
            raise ValueError("invalid notification plan")
        parsed.append(NotificationMessage(title, body, url))
    return NotificationPlan(tuple(parsed))


def build_notification_plan(
    *,
    candidates: int,
    processed: int,
    selected: int,
    focus_selected: int,
    base_url: str,
    recommendation: Recommendation | None,
    recommendation_record: PaperRecord | None,
    recommendation_failed: bool,
) -> NotificationPlan:
    counts = (candidates, processed, selected, focus_selected)
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("invalid notification plan")
    summary_url = f"{base_url.rstrip('/')}/ai_summary.html"
    if not _is_http_url(summary_url):
        raise ValueError("invalid notification plan")

    statistics = NotificationMessage(
        "金刚石文献日报",
        (
            f"今日候选：{candidates} 篇\n"
            f"完成筛选：{processed} 篇\n"
            f"综合入选：{selected} 篇\n"
            f"器件方向：{focus_selected} 篇"
        ),
        summary_url,
    )
    if recommendation_failed:
        if recommendation is not None or recommendation_record is not None:
            raise ValueError("invalid notification plan")
        recommendation_message = NotificationMessage(
            "今日论文推荐",
            "今日推荐生成失败，器件方向 RSS 已正常更新",
            None,
        )
    elif focus_selected == 0:
        if recommendation is not None or recommendation_record is not None:
            raise ValueError("invalid notification plan")
        recommendation_message = NotificationMessage(
            "今日论文推荐", "今日无器件方向推荐", None
        )
    else:
        if (
            recommendation is None
            or recommendation_record is None
            or recommendation.key != record_key(recommendation_record)
            or not focus_names(recommendation_record)
            or not _is_http_url(recommendation_record.url)
        ):
            raise ValueError("invalid notification plan")
        recommendation_message = NotificationMessage(
            "今日论文推荐",
            (
                f"{recommendation_record.title}\n"
                f"方向：{'、'.join(focus_names(recommendation_record))}\n"
                f"推荐理由：{recommendation.reason}"
            ),
            recommendation_record.url,
        )
    return NotificationPlan((statistics, recommendation_message))


def write_notification_plan(path: Path, plan: NotificationPlan) -> None:
    payload = {
        "version": 1,
        "messages": [
            {"title": message.title, "body": message.body, "url": message.url}
            for message in plan.messages
        ],
    }
    validated = _validated_plan(payload)
    contents = json.dumps(
        {
            "version": 1,
            "messages": [
                {"title": item.title, "body": item.body, "url": item.url}
                for item in validated.messages
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"
    atomic_write_text(Path(path), contents)


def load_notification_plan(path: Path) -> NotificationPlan:
    try:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
        return _validated_plan(raw)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        _DuplicateKeyError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError("invalid notification plan") from error
