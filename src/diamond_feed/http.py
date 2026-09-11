from dataclasses import dataclass
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from curl_cffi import requests as curl_requests

from diamond_feed.retry import RetryPolicy, call_with_retry, retryable_http_status


USER_AGENT = "diamond-paper-feed/0.1 (resilient RSS collector)"


@dataclass(frozen=True, slots=True)
class HttpResult:
    body: bytes
    status: int
    final_url: str


class FetchError(Exception):
    def __init__(
        self,
        *,
        status: int | None,
        category: str,
        detail: str,
        retryable: bool = False,
    ):
        super().__init__(detail)
        self.status = status
        self.category = category
        self.detail = detail
        self.retryable = retryable


def _transport_failure(error: Exception) -> tuple[str, bool]:
    if isinstance(error, curl_requests.exceptions.Timeout):
        return "timeout", True
    if isinstance(error, curl_requests.exceptions.SSLError):
        return "network_error", False
    if isinstance(error, curl_requests.exceptions.ConnectionError):
        return "network_error", True
    if isinstance(error, TimeoutError):
        return "timeout", True
    if isinstance(error, URLError):
        reason = getattr(error, "reason", None)
        if isinstance(reason, TimeoutError):
            return "timeout", True
        if isinstance(reason, socket.gaierror):
            return "url_error", reason.errno == socket.EAI_AGAIN
        if isinstance(reason, ConnectionError):
            return "url_error", True
        return "url_error", False
    return "network_error", False


def _fetch_with_urllib(url: str, timeout: float) -> HttpResult:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return HttpResult(
            body=response.read(),
            status=response.getcode(),
            final_url=response.geturl(),
        )


def _fetch_with_mdpi(url: str, timeout: float) -> HttpResult:
    response = curl_requests.get(
        url,
        timeout=timeout,
        impersonate="chrome",
        allow_redirects=True,
    )
    return HttpResult(body=response.content, status=response.status_code, final_url=str(response.url))


def fetch_bytes(url: str, timeout: float, attempts: int) -> HttpResult:
    """Fetch bytes with bounded retries for transient feed failures."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    policy = RetryPolicy(
        attempts=attempts,
        delays=tuple(float(2**index) for index in range(attempts - 1)),
    )
    fetch_once = _fetch_with_mdpi if "www.mdpi.com" in url else _fetch_with_urllib

    def operation() -> HttpResult:
        try:
            result = fetch_once(url, timeout)
        except HTTPError as error:
            raise FetchError(
                status=error.code,
                category=f"http_{error.code}",
                detail=f"HTTP {error.code}",
                retryable=retryable_http_status(error.code),
            ) from error
        except Exception as error:
            category, retryable = _transport_failure(error)
            raise FetchError(
                status=None,
                category=category,
                detail=category,
                retryable=retryable,
            ) from error
        if result.status >= 400:
            raise FetchError(
                status=result.status,
                category=f"http_{result.status}",
                detail=f"HTTP {result.status}",
                retryable=retryable_http_status(result.status),
            )
        return result

    return call_with_retry(
        operation,
        lambda error: isinstance(error, FetchError) and error.retryable,
        policy=policy,
        wait=time.sleep,
    )
