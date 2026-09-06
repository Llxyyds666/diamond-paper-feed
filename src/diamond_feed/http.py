from dataclasses import dataclass
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from curl_cffi import requests as curl_requests


USER_AGENT = "diamond-paper-feed/0.1 (resilient RSS collector)"


@dataclass(frozen=True, slots=True)
class HttpResult:
    body: bytes
    status: int
    final_url: str


class FetchError(Exception):
    def __init__(self, *, status: int | None, category: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.category = category
        self.detail = detail


def _retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _transport_failure(error: Exception) -> tuple[str, bool]:
    if isinstance(error, curl_requests.exceptions.Timeout):
        return "timeout", True
    if isinstance(error, curl_requests.exceptions.ConnectionError):
        return "network_error", True
    if isinstance(error, TimeoutError):
        return "timeout", True
    if isinstance(error, URLError):
        reason = getattr(error, "reason", None)
        if isinstance(reason, TimeoutError):
            return "timeout", True
        if isinstance(reason, (socket.gaierror, ConnectionError)):
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

    fetch_once = _fetch_with_mdpi if "www.mdpi.com" in url else _fetch_with_urllib
    for attempt in range(attempts):
        try:
            result = fetch_once(url, timeout)
            if _retryable_status(result.status):
                raise FetchError(
                    status=result.status,
                    category=f"http_{result.status}",
                    detail=f"HTTP {result.status}",
                )
            if result.status >= 400:
                raise FetchError(
                    status=result.status,
                    category=f"http_{result.status}",
                    detail=f"HTTP {result.status}",
                )
            return result
        except HTTPError as error:
            failure = FetchError(
                status=error.code,
                category=f"http_{error.code}",
                detail=f"HTTP {error.code}",
            )
        except FetchError as error:
            failure = error
        except Exception as error:
            category, retryable = _transport_failure(error)
            failure = FetchError(status=None, category=category, detail=category)
            if not retryable:
                raise failure from error

        retryable = failure.status is None or _retryable_status(failure.status)
        if not retryable or attempt == attempts - 1:
            raise failure
        time.sleep(2**attempt)

    raise AssertionError("unreachable")
