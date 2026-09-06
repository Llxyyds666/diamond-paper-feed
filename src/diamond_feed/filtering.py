from dataclasses import dataclass
import json
from pathlib import Path
import re
import unicodedata

from diamond_feed.models import PaperRecord


@dataclass(frozen=True, slots=True)
class QueryRules:
    include_any: list[str]
    material_context: list[str]
    obvious_noise: list[str]


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()


def load_rules(path: Path) -> QueryRules:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return QueryRules(
        include_any=[_normalize_text(str(value)) for value in raw["include_any"]],
        material_context=[_normalize_text(str(value)) for value in raw["material_context"]],
        obvious_noise=[_normalize_text(str(value)) for value in raw["obvious_noise"]],
    )


def matches_rules(record: PaperRecord, rules: QueryRules) -> bool:
    text = _normalize_text(f"{record.title} {record.abstract}")
    if not any(term in text for term in rules.include_any):
        return False
    has_material_context = any(term in text for term in rules.material_context)
    has_obvious_noise = any(term in text for term in rules.obvious_noise)
    return has_material_context or not has_obvious_noise
