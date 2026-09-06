from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(slots=True)
class PaperRecord:
    title: str
    abstract: str
    authors: list[str]
    journal: str
    published_at: datetime
    doi: str | None
    url: str
    sources: list[str]
    source_ids: list[str]
    categories: list[str] = field(default_factory=list)
    summary_zh: str | None = None
    ai_relevant: bool | None = None
    ai_confidence: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "abstract": self.abstract,
            "authors": list(self.authors),
            "journal": self.journal,
            "published_at": _utc_timestamp(self.published_at).isoformat(),
            "doi": self.doi,
            "url": self.url,
            "sources": list(self.sources),
            "source_ids": list(self.source_ids),
            "categories": list(self.categories),
            "summary_zh": self.summary_zh,
            "ai_relevant": self.ai_relevant,
            "ai_confidence": self.ai_confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PaperRecord":
        published_at = datetime.fromisoformat(str(data["published_at"]))
        return cls(
            title=str(data["title"]),
            abstract=str(data["abstract"]),
            authors=[str(value) for value in data["authors"]],  # type: ignore[index, union-attr]
            journal=str(data["journal"]),
            published_at=_utc_timestamp(published_at),
            doi=None if data.get("doi") is None else str(data["doi"]),
            url=str(data["url"]),
            sources=[str(value) for value in data["sources"]],  # type: ignore[index, union-attr]
            source_ids=[str(value) for value in data["source_ids"]],  # type: ignore[index, union-attr]
            categories=[str(value) for value in data.get("categories", [])],  # type: ignore[arg-type]
            summary_zh=None if data.get("summary_zh") is None else str(data["summary_zh"]),
            ai_relevant=None if data.get("ai_relevant") is None else bool(data["ai_relevant"]),
            ai_confidence=None
            if data.get("ai_confidence") is None
            else float(data["ai_confidence"]),
        )


@dataclass(frozen=True, slots=True)
class SourceFailure:
    timestamp: datetime
    category: str
    url: str
    detail: str


@dataclass(frozen=True, slots=True)
class AiDecision:
    key: str
    relevant: bool
    confidence: float
    category: str
    matched_topics: list[str]
    summary_zh: str
    reason: str
