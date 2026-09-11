"""Official Bark delivery with bounded retries per planned message."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from diamond_feed.notification import NotificationPlan
from diamond_feed.retry import (
    DEFAULT_RETRY_POLICY,
    RetryPolicy,
    call_with_retry,
    retryable_http_status,
)


BARK_URL = "https://api.day.app/push"
BARK_GROUP = "diamond-paper-feed"
BARK_ICON_URL = (
    "https://llxyyds666.github.io/diamond-paper-feed/"
    "assets/diamond-bark-icon.png"
)


@dataclass(frozen=True, slots=True)
class BarkResponse:
    status: int
    body: bytes


BarkTransport = Callable[[str, Mapping[str, str], bytes, float], BarkResponse]


class BarkProtocolError(ValueError):
    """The Bark service response did not satisfy the required contract."""

    def __init__(self, *, status: int | None, retryable: bool):
        super().__init__("invalid Bark response")
        self.status = status
        self.retryable = retryable


class _DuplicateKeyError(ValueError):
    pass


def _reject_json_constant(value: str) -> object:
    raise ValueError("invalid JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> BarkResponse:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with build_opener(_NoRedirectHandler()).open(
            request, timeout=timeout
        ) as response:
            return BarkResponse(response.status, response.read())
    except HTTPError as error:
        return BarkResponse(error.code, error.read())


def _validate_response(response: BarkResponse) -> None:
    if type(response.status) is not int or not 200 <= response.status < 300:
        status = response.status if type(response.status) is int else None
        raise BarkProtocolError(
            status=status,
            retryable=status is not None and retryable_http_status(status),
        )
    try:
        payload = json.loads(
            response.body,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, TypeError, ValueError) as error:
        raise BarkProtocolError(status=response.status, retryable=True) from error
    if type(payload) is not dict or type(payload.get("code")) is not int:
        raise BarkProtocolError(status=response.status, retryable=True)
    if payload["code"] != 200:
        raise BarkProtocolError(status=response.status, retryable=True)


def send_plan(
    token: str,
    plan: NotificationPlan,
    *,
    transport: BarkTransport | None = None,
    timeout_seconds: float = 10.0,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    wait: Callable[[float], None] = time.sleep,
) -> tuple[bool, ...]:
    """Deliver every message independently with bounded transient retries."""
    sender = transport or _urllib_transport
    results: list[bool] = []
    for index, message in enumerate(plan.messages, start=1):
        payload = {
            "device_key": token,
            "title": message.title,
            "body": message.body,
            "group": BARK_GROUP,
            "icon": BARK_ICON_URL,
        }
        if message.url is not None:
            payload["url"] = message.url
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def operation() -> None:
            response = sender(
                BARK_URL,
                {"Content-Type": "application/json"},
                body,
                timeout_seconds,
            )
            _validate_response(response)

        def should_retry(error: Exception) -> bool:
            if isinstance(error, BarkProtocolError):
                return error.retryable
            if isinstance(error, HTTPError):
                return retryable_http_status(error.code)
            return isinstance(error, (TimeoutError, ConnectionError, URLError))

        try:
            call_with_retry(
                operation,
                should_retry,
                policy=policy,
                wait=wait,
            )
        except Exception as error:
            status = getattr(error, "status", None)
            if isinstance(error, HTTPError):
                status = error.code
            status_text = status if status is not None else "none"
            print(
                f"Bark notification {index} failed: "
                f"{type(error).__name__} status={status_text}",
                file=sys.stderr,
            )
            results.append(False)
        else:
            results.append(True)
    return tuple(results)
