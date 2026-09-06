from dataclasses import dataclass
from pathlib import Path
import json
import math


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


def load_config(path: Path) -> AppConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    collection_raw = raw["collection"]
    ai_raw = raw["ai"]
    publication_raw = raw["publication"]
    collection = CollectionConfig(
        lookback_days=_positive("lookback_days", collection_raw["lookback_days"]),
        raw_feed_max_items=_positive("raw_feed_max_items", collection_raw["raw_feed_max_items"]),
        http_timeout_seconds=_positive("http_timeout_seconds", collection_raw["http_timeout_seconds"]),
        http_attempts=_positive("http_attempts", collection_raw["http_attempts"]),
    )
    ai = AiConfig(
        base_url=str(ai_raw["base_url"]).rstrip("/"),
        model=str(ai_raw["model"]),
        daily_candidates=_positive("daily_candidates", ai_raw["daily_candidates"]),
        batch_size=_positive("batch_size", ai_raw["batch_size"]),
        max_requests=_positive("max_requests", ai_raw["max_requests"]),
        max_abstract_chars=_positive("max_abstract_chars", ai_raw["max_abstract_chars"]),
        screening_max_tokens=_positive("screening_max_tokens", ai_raw["screening_max_tokens"]),
        digest_max_tokens=_positive("digest_max_tokens", ai_raw["digest_max_tokens"]),
    )
    if not ai.base_url.startswith("https://"):
        raise ValueError("AI base_url must use HTTPS")
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
