from dataclasses import dataclass
from pathlib import Path
import json
import math


AI_BASE_URL = "https://api.deepseek.com"
AI_MODEL = "deepseek-v4-flash-vision-exp"
AI_LIMITS = {
    "daily_candidates": 40,
    "batch_size": 10,
    "max_requests": 5,
    "max_abstract_chars": 1200,
    "screening_max_tokens": 4096,
    "digest_max_tokens": 8192,
}

ROOT_KEYS = {"collection", "ai", "publication"}
COLLECTION_KEYS = {
    "lookback_days",
    "raw_feed_max_items",
    "http_timeout_seconds",
    "http_attempts",
}
AI_KEYS = {"base_url", "model", *AI_LIMITS}
PUBLICATION_KEYS = {"title", "base_url"}


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    lookback_days: int
    raw_feed_max_items: int
    http_timeout_seconds: int
    http_attempts: int


@dataclass(frozen=True, slots=True)
class AiConfig:
    base_url: str
    model: str
    daily_candidates: int
    batch_size: int
    max_requests: int
    max_abstract_chars: int
    screening_max_tokens: int
    digest_max_tokens: int


@dataclass(frozen=True, slots=True)
class PublicationConfig:
    title: str
    base_url: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    collection: CollectionConfig
    ai: AiConfig
    publication: PublicationConfig


def _positive(name: str, value: object) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _bounded_positive(name: str, value: object) -> int:
    parsed = _positive(name, value)
    maximum = AI_LIMITS[name]
    if parsed > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")
    return parsed


def _reject_unknown_keys(section: str, values: dict[str, object], expected: set[str]) -> None:
    unknown = set(values) - expected
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"{section} has unknown configuration key(s): {names}")


def load_config(path: Path) -> AppConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    _reject_unknown_keys("root", raw, ROOT_KEYS)
    collection_raw = raw["collection"]
    ai_raw = raw["ai"]
    publication_raw = raw["publication"]
    _reject_unknown_keys("collection", collection_raw, COLLECTION_KEYS)
    _reject_unknown_keys("ai", ai_raw, AI_KEYS)
    _reject_unknown_keys("publication", publication_raw, PUBLICATION_KEYS)
    collection = CollectionConfig(
        lookback_days=_positive("lookback_days", collection_raw["lookback_days"]),
        raw_feed_max_items=_positive("raw_feed_max_items", collection_raw["raw_feed_max_items"]),
        http_timeout_seconds=_positive("http_timeout_seconds", collection_raw["http_timeout_seconds"]),
        http_attempts=_positive("http_attempts", collection_raw["http_attempts"]),
    )
    ai = AiConfig(
        base_url=str(ai_raw["base_url"]).rstrip("/"),
        model=str(ai_raw["model"]),
        daily_candidates=_bounded_positive("daily_candidates", ai_raw["daily_candidates"]),
        batch_size=_bounded_positive("batch_size", ai_raw["batch_size"]),
        max_requests=_bounded_positive("max_requests", ai_raw["max_requests"]),
        max_abstract_chars=_bounded_positive("max_abstract_chars", ai_raw["max_abstract_chars"]),
        screening_max_tokens=_bounded_positive("screening_max_tokens", ai_raw["screening_max_tokens"]),
        digest_max_tokens=_bounded_positive("digest_max_tokens", ai_raw["digest_max_tokens"]),
    )
    if ai.base_url != AI_BASE_URL:
        raise ValueError(f"AI base_url must match {AI_BASE_URL}")
    if ai.model != AI_MODEL:
        raise ValueError(f"AI model must match {AI_MODEL}")
    if math.ceil(ai.daily_candidates / ai.batch_size) + 1 > ai.max_requests:
        raise ValueError("max_requests must reserve one request for the digest")
    return AppConfig(
        collection=collection,
        ai=ai,
        publication=PublicationConfig(
            title=str(publication_raw["title"]),
            base_url=str(publication_raw["base_url"]).rstrip("/"),
        ),
    )
