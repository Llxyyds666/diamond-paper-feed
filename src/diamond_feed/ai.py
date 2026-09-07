"""Bounded, secret-safe DeepSeek screening client."""

import json
import math
import socket
from collections.abc import Callable, Sequence
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from diamond_feed.config import AiConfig
from diamond_feed.models import AiDecision, PaperRecord
from diamond_feed.normalize import record_key


DECISION_FIELDS = {
    "key",
    "relevant",
    "confidence",
    "category",
    "matched_topics",
    "summary_zh",
    "reason",
}
CATEGORIES = {
    "growth-processing",
    "films-membranes",
    "doping-defects",
    "quantum-color-centers",
    "electronics-optoelectronics",
    "thermal-mechanical-acoustic",
    "piezoelectric-sensing",
    "electrochemistry-catalysis",
    "nanodiamond-biomedical",
    "natural-diamond-geoscience",
    "adjacent-dlc",
    "other-diamond",
}
DEFAULT_TIMEOUT_SECONDS = 30.0

Transport = Callable[[str, dict[str, str], dict[str, object], float], object]


class RequestBudget:
    """A thread-safe count of attempts, not just successful requests."""

    def __init__(self, maximum: int):
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("maximum must be a positive integer")
        self._maximum = maximum
        self._used = 0
        self._lock = Lock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._maximum - self._used

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def consume(self) -> None:
        """Reserve an attempt before any network operation begins."""
        with self._lock:
            if self._used >= self._maximum:
                raise RuntimeError("request budget exhausted")
            self._used += 1


class _ModelResponseError(ValueError):
    pass


def _duplicate_safe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise _ModelResponseError
        result[name] = value
    return result


def _decode_model_json(content: str) -> list[object] | dict[str, object]:
    try:
        decoded = json.loads(content, object_pairs_hook=_duplicate_safe_object)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise _ModelResponseError from error
    if type(decoded) not in (list, dict):
        raise _ModelResponseError
    return decoded


def _extract_model_json(response: object) -> list[object] | dict[str, object]:
    if type(response) is not dict:
        raise _ModelResponseError
    choices = response.get("choices")
    if type(choices) is not list or not choices:
        raise _ModelResponseError
    first = choices[0]
    if type(first) is not dict:
        raise _ModelResponseError
    message = first.get("message")
    if type(message) is not dict:
        raise _ModelResponseError
    content = message.get("content")
    if type(content) is not str:
        raise _ModelResponseError
    return _decode_model_json(content)


def _response_usage(response: object) -> tuple[int, int] | None:
    if type(response) is not dict or type(response.get("usage")) is not dict:
        return None
    usage = response["usage"]
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if (
        type(prompt) is not int
        or prompt < 0
        or type(completion) is not int
        or completion < 0
    ):
        return None
    return prompt, completion


def _http_status(error: Exception) -> int | None:
    if isinstance(error, HTTPError) and type(error.code) is int:
        return error.code
    return None


def _is_timeout(error: Exception) -> bool:
    if isinstance(error, TimeoutError):
        return True
    return isinstance(error, URLError) and isinstance(getattr(error, "reason", None), (TimeoutError, socket.timeout))


def _retryable(error: Exception) -> bool:
    status = _http_status(error)
    return _is_timeout(error) or status == 429 or (status is not None and 500 <= status <= 599)


def _safe_request_error(error: Exception, attempt: int) -> RuntimeError:
    status = _http_status(error)
    status_text = "none" if status is None else str(status)
    return RuntimeError(f"{type(error).__name__} status={status_text} attempt={attempt}")


class DeepSeekClient:
    """Small OpenAI-compatible client which never formats credentials in errors."""

    def __init__(self, key: str, config: AiConfig, *, transport: Transport | None = None):
        self._key = key
        self._config = config
        self._transport = transport or self._default_transport
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._usage_lock = Lock()

    @property
    def prompt_tokens(self) -> int:
        with self._usage_lock:
            return self._prompt_tokens

    @property
    def completion_tokens(self) -> int:
        with self._usage_lock:
            return self._completion_tokens

    @property
    def total_tokens(self) -> int:
        with self._usage_lock:
            return self._prompt_tokens + self._completion_tokens

    def _default_transport(
        self, url: str, headers: dict[str, str], payload: dict[str, object], timeout: float
    ) -> object:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=encoded, headers=headers, method="POST")
        with urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            if type(status) is int and status >= 400:
                raise HTTPError(url, status, "", None, None)
            try:
                return json.loads(response.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise _ModelResponseError from error

    def complete_json(
        self, messages: Sequence[dict[str, object]], max_tokens: int, budget: RequestBudget
    ) -> object:
        """Send one request or retry a transient transport failure within ``budget``."""
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": list(messages),
            "stream": False,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        attempt = 0
        while True:
            budget.consume()
            attempt += 1
            try:
                response = self._transport(url, headers, payload, DEFAULT_TIMEOUT_SECONDS)
            except _ModelResponseError:
                raise ValueError("invalid model response") from None
            except Exception as error:
                if _retryable(error):
                    continue
                raise _safe_request_error(error, attempt) from None
            usage = _response_usage(response)
            if usage is not None:
                with self._usage_lock:
                    self._prompt_tokens += usage[0]
                    self._completion_tokens += usage[1]
            try:
                result = _extract_model_json(response)
            except _ModelResponseError:
                raise ValueError("invalid model response") from None
            return result


def _screening_messages(records: Sequence[PaperRecord], config: AiConfig) -> list[dict[str, object]]:
    papers = [
        {
            "key": record_key(record),
            "title": record.title,
            "abstract": record.abstract[: config.max_abstract_chars],
            "authors": list(record.authors),
            "journal": record.journal,
            "published_at": record.published_at.isoformat(),
            "doi": record.doi,
            "url": record.url,
        }
        for record in records
    ]
    instructions = {
        "papers": papers,
        "categories": sorted(CATEGORIES),
        "required_fields": sorted(DECISION_FIELDS),
    }
    return [
        {
            "role": "system",
            "content": "Return only a JSON array with one strict decision for each requested paper.",
        },
        {"role": "user", "content": json.dumps(instructions, ensure_ascii=False)},
    ]


def _is_string_list(value: object) -> bool:
    return type(value) is list and all(type(item) is str for item in value)


def _validated_decision(value: object) -> AiDecision:
    if type(value) is not dict or set(value) != DECISION_FIELDS:
        raise _ModelResponseError
    key = value["key"]
    relevant = value["relevant"]
    confidence = value["confidence"]
    category = value["category"]
    matched_topics = value["matched_topics"]
    summary_zh = value["summary_zh"]
    reason = value["reason"]
    if type(key) is not str or type(relevant) is not bool:
        raise _ModelResponseError
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise _ModelResponseError
    if type(category) is not str or category not in CATEGORIES:
        raise _ModelResponseError
    if not _is_string_list(matched_topics) or type(summary_zh) is not str or type(reason) is not str:
        raise _ModelResponseError
    return AiDecision(
        key=key,
        relevant=relevant,
        confidence=float(confidence),
        category=category,
        matched_topics=list(matched_topics),
        summary_zh=summary_zh,
        reason=reason,
    )


def screen_batch(
    records: Sequence[PaperRecord], client: DeepSeekClient, config: AiConfig, budget: RequestBudget
) -> list[AiDecision]:
    """Screen one bounded record batch and reject invalid model output atomically."""
    if not records:
        return []
    if len(records) > config.batch_size:
        raise ValueError("batch size exceeds configured limit")
    requested_keys = [record_key(record) for record in records]
    if len(set(requested_keys)) != len(requested_keys):
        raise ValueError("invalid screening batch")
    raw = client.complete_json(_screening_messages(records, config), config.screening_max_tokens, budget)
    if type(raw) is not list:
        raise ValueError("invalid model response")
    try:
        decisions = [_validated_decision(item) for item in raw]
    except _ModelResponseError:
        raise ValueError("invalid model response") from None
    returned_keys = [decision.key for decision in decisions]
    if len(returned_keys) != len(set(returned_keys)) or set(returned_keys) != set(requested_keys):
        raise ValueError("invalid model response")
    return decisions
