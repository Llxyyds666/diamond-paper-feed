# Reliable Feed Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Process up to 100 papers per day without a global DeepSeek request ceiling and retry every transient application HTTP failure three times.

**Architecture:** Add one generic bounded retry primitive and keep protocol-specific status/error classification at each integration boundary. Retain `RequestBudget` for deliberately bounded tests and add an unlimited `RequestCounter` for production summary runs. Preserve both RSS feeds' current identity because the Zotero incident was confirmed to be a stalled local refresh scheduler.

**Tech Stack:** Python 3.11+, standard-library `urllib`, `curl_cffi`, pytest, RSS 2.0/XML, GitHub Actions, GitHub Pages.

## Global Constraints

- A normal daily summary run processes at most 100 pending candidate papers per UTC day.
- DeepSeek has no global daily request-attempt cap; each logical HTTP operation still has exactly three attempts at most.
- Retry waits are one second and two seconds before attempts two and three.
- Retry HTTP 408, 425, 429, and 5xx plus connection failures, timeouts, and malformed DeepSeek/Bark responses.
- Do not retry permanent authentication, balance, not-found, or invalid-parameter responses.
- Never log secrets, response bodies, or credential-bearing URLs.
- Preserve the current three device-focus definitions, source list, and 40-paper isolated evaluation sample.
- The existing uncommitted recommendation-priority regression changes in `src/diamond_feed/recommend.py`, `src/diamond_feed/summarize.py`, `tests/test_recommend.py`, and `tests/test_summarize.py` belong to Task 4 and must not be discarded.

---

### Task 1: Shared bounded retry primitive

**Files:**
- Create: `src/diamond_feed/retry.py`
- Create: `tests/test_retry.py`

**Interfaces:**
- Produces: `RetryPolicy`, `DEFAULT_RETRY_POLICY`, `retryable_http_status(status: int) -> bool`, and `call_with_retry(operation, should_retry, *, policy, wait)`.
- Consumers: collection HTTP, enrichment, DeepSeek, Bark, and evaluation integrations in Tasks 2–6.

- [ ] **Step 1: Write failing retry-policy tests**

```python
from urllib.error import HTTPError

import pytest

from diamond_feed.retry import (
    DEFAULT_RETRY_POLICY,
    call_with_retry,
    retryable_http_status,
)


def test_transient_operation_uses_three_attempts_with_exponential_waits():
    attempts = []
    waits = []

    def operation():
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise TimeoutError("temporary")
        return "ok"

    result = call_with_retry(
        operation,
        lambda error: isinstance(error, TimeoutError),
        policy=DEFAULT_RETRY_POLICY,
        wait=waits.append,
    )

    assert result == "ok"
    assert attempts == [1, 2, 3]
    assert waits == [1.0, 2.0]


def test_permanent_operation_is_attempted_once():
    attempts = []

    def operation():
        attempts.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        call_with_retry(
            operation,
            lambda error: isinstance(error, TimeoutError),
            policy=DEFAULT_RETRY_POLICY,
            wait=lambda seconds: pytest.fail("must not wait"),
        )
    assert len(attempts) == 1


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 599])
def test_retryable_http_statuses(status):
    assert retryable_http_status(status) is True


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 422])
def test_permanent_http_statuses(status):
    assert retryable_http_status(status) is False
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `python -m pytest -q tests/test_retry.py`

Expected: collection fails because `diamond_feed.retry` does not exist.

- [ ] **Step 3: Implement the minimal shared retry module**

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int
    delays: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.attempts < 1 or len(self.delays) != self.attempts - 1:
            raise ValueError("invalid retry policy")
        if any(delay < 0 for delay in self.delays):
            raise ValueError("invalid retry policy")


DEFAULT_RETRY_POLICY = RetryPolicy(3, (1.0, 2.0))


def retryable_http_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status <= 599


def call_with_retry(
    operation: Callable[[], T],
    should_retry: Callable[[Exception], bool],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    wait: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(policy.attempts):
        try:
            return operation()
        except Exception as error:
            if attempt == policy.attempts - 1 or not should_retry(error):
                raise
            wait(policy.delays[attempt])
    raise AssertionError("unreachable")
```

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest -q tests/test_retry.py`

Expected: all retry-policy tests pass without real sleeping.

- [ ] **Step 5: Commit**

```bash
git add src/diamond_feed/retry.py tests/test_retry.py
git commit -m "feat: add shared bounded retry policy"
```

---

### Task 2: Migrate collection HTTP to the shared policy

**Files:**
- Modify: `src/diamond_feed/http.py`
- Modify: `tests/test_rss_source.py`

**Interfaces:**
- Consumes: `call_with_retry`, `RetryPolicy`, and `retryable_http_status` from Task 1.
- Preserves: `fetch_bytes(url: str, timeout: float, attempts: int) -> HttpResult` and existing `FetchError` categories.

- [ ] **Step 1: Extend failing collection tests to cover 408 and 425 and injected waits**

```python
@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_fetch_bytes_retries_retryable_http_statuses(monkeypatch, status):
    calls = []
    waits = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, status, "temporary", None, None)

    monkeypatch.setattr("diamond_feed.http.urlopen", fake_urlopen)
    monkeypatch.setattr("diamond_feed.http.time.sleep", waits.append)

    with pytest.raises(FetchError) as raised:
        fetch_bytes("https://feed.test/rss", timeout=3, attempts=3)

    assert raised.value.status == status
    assert len(calls) == 3
    assert waits == [1.0, 2.0]
```

- [ ] **Step 2: Verify RED for the newly supported statuses**

Run: `python -m pytest -q tests/test_rss_source.py`

Expected: the 408/425 cases show only one attempt under the current implementation.

- [ ] **Step 3: Replace local status/backoff decisions with the shared policy**

Keep `_transport_failure()` because it maps publisher-library exceptions into stable failure categories. Import `RetryPolicy`, `call_with_retry`, and `retryable_http_status`; configure `RetryPolicy(attempts, tuple(2**index for index in range(attempts - 1)))`; wrap a single fetch attempt so retryable HTTP results raise `FetchError`; and use `call_with_retry()` with a predicate that checks `FetchError.status`, transient transport categories, and `retryable_http_status()`.

The resulting public function remains:

```python
def fetch_bytes(url: str, timeout: float, attempts: int) -> HttpResult:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    policy = RetryPolicy(attempts, tuple(float(2**i) for i in range(attempts - 1)))
    fetch_once = _fetch_with_mdpi if "www.mdpi.com" in url else _fetch_with_urllib

    def operation() -> HttpResult:
        try:
            result = fetch_once(url, timeout)
        except HTTPError as error:
            raise FetchError(
                status=error.code,
                category=f"http_{error.code}",
                detail=f"HTTP {error.code}",
            ) from error
        except Exception as error:
            category, retryable = _transport_failure(error)
            failure = FetchError(status=None, category=category, detail=category)
            failure.retryable = retryable
            raise failure from error
        if result.status >= 400:
            failure = FetchError(
                status=result.status,
                category=f"http_{result.status}",
                detail=f"HTTP {result.status}",
            )
            failure.retryable = retryable_http_status(result.status)
            raise failure
        return result

    def should_retry(error: Exception) -> bool:
        if not isinstance(error, FetchError):
            return False
        if error.status is not None:
            return retryable_http_status(error.status)
        return bool(getattr(error, "retryable", False))

    return call_with_retry(operation, should_retry, policy=policy)
```

If implementation review rejects a dynamic `retryable` attribute, add `retryable: bool = False` as an explicit `FetchError` constructor field and update existing construction sites.

- [ ] **Step 4: Verify collection behavior**

Run: `python -m pytest -q tests/test_rss_source.py tests/test_source_tools.py tests/test_state_collect_render.py`

Expected: all collection/retry/failure-classification tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/diamond_feed/http.py tests/test_rss_source.py
git commit -m "refactor: share collection retry policy"
```

---

### Task 3: Retry Semantic Scholar and OpenAIRE enrichment

**Files:**
- Modify: `src/diamond_feed/enrich.py`
- Modify: `tests/test_enrich.py`

**Interfaces:**
- Consumes: `call_with_retry`, `DEFAULT_RETRY_POLICY`, and `retryable_http_status`.
- Extends: `AbstractEnricher(..., wait: Callable[[float], None] = time.sleep)` for testable backoff.

- [ ] **Step 1: Write failing enrichment retry tests**

Add a sequence transport that returns `MetadataResponse(503, b"")`, then a valid response. Assert two calls and `[1.0]` waits. Add a 401 case and assert one call/no waits. Add malformed JSON followed by valid JSON and assert it retries.

```python
def test_enrichment_retries_transient_provider_response(
    state_with_missing_doi, monkeypatch
):
    responses = iter([
        MetadataResponse(503, b""),
        MetadataResponse(200, VALID_SEMANTIC_RESPONSE),
    ])
    calls = []
    waits = []

    def transport(*args):
        calls.append(args[1])
        return next(responses)

    enricher = AbstractEnricher(
        "semantic-secret", transport=transport, wait=waits.append
    )
    stats = enricher.enrich(state_with_missing_doi, list(state_with_missing_doi.papers))

    assert stats.enriched == 1
    assert len(calls) == 2
    assert waits == [1.0]
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_enrich.py`

Expected: constructor rejects `wait` or the provider is called only once.

- [ ] **Step 3: Retry one provider request while keeping fallback semantics**

In `_request_json`, move transport, HTTP status validation, and `json.loads()` into one `operation()`. Retry `HTTPError` only when its status is retryable; retry `TimeoutError`, `ConnectionError`, `URLError`, and `json.JSONDecodeError`; report one sanitized error only after the final attempt. Do not include bodies or URLs in the error log.

```python
payload = call_with_retry(
    operation,
    lambda error: (
        isinstance(error, HTTPError) and retryable_http_status(error.code)
    ) or isinstance(
        error,
        (TimeoutError, ConnectionError, URLError, json.JSONDecodeError),
    ),
    policy=DEFAULT_RETRY_POLICY,
    wait=self._wait,
)
```

- [ ] **Step 4: Verify enrichment tests**

Run: `python -m pytest -q tests/test_enrich.py tests/test_summarize.py`

Expected: enrichment retries pass and summary enrichment behavior remains green.

- [ ] **Step 5: Commit**

```bash
git add src/diamond_feed/enrich.py tests/test_enrich.py
git commit -m "feat: retry abstract enrichment providers"
```

---

### Task 4: Remove the DeepSeek request ceiling and prioritize recommendations

**Files:**
- Modify: `src/diamond_feed/ai.py`
- Modify: `src/diamond_feed/config.py`
- Modify: `src/diamond_feed/recommend.py`
- Modify: `src/diamond_feed/summarize.py`
- Modify: `paper_feed_config.json`
- Modify: `.github/workflows/summarize.yml`
- Modify: `tests/test_ai.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_recommend.py`
- Modify: `tests/test_summarize.py`
- Modify: `tests/test_workflows.py`
- Modify: `tests/test_acceptance.py`

**Interfaces:**
- Produces: `RequestCounter` with `used` and `consume()`; production uses it without a maximum.
- Preserves: `RequestBudget(maximum)` as a bounded subclass/test utility.
- Changes: `AiConfig` removes `max_requests`; `daily_candidates` maximum/default becomes 100.
- Consumes: shared retry policy from Task 1.

- [ ] **Step 1: Finish the failing throughput and retry tests**

Retain the already-written RED/GREEN tests that require recommendation before overview and permit a second recommendation attempt. Add:

```python
def test_request_counter_never_exhausts():
    counter = RequestCounter()
    for _ in range(1000):
        counter.consume()
    assert counter.used == 1000


def test_deepseek_retries_timeout_and_invalid_json_three_times(ai_config):
    attempts = []
    waits = []

    def transport(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("temporary")
        if len(attempts) == 2:
            return {"choices": [{"message": {"content": "not-json"}}]}
        return VALID_RESPONSE

    client = DeepSeekClient(
        "test-api-key", ai_config, transport=transport, wait=waits.append
    )
    result = client.complete_json(MESSAGES, 512, RequestCounter())
    assert result == EXPECTED_RESULT
    assert len(attempts) == 3
    assert waits == [1.0, 2.0]


def test_daily_summary_processes_one_hundred_candidates_without_request_ceiling(...):
    records = make_records(100)
    stats = run_summary(...)
    assert stats.processed == 100
    assert stats.requests >= 10
    assert len(state.pending_ai) == 0
```

Update configuration tests to reject values above 100 but require no `max_requests` key. Update workflow tests so the smoke test uses `RequestCounter()` and retains one logical smoke operation with the common three-attempt policy.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest -q tests/test_ai.py tests/test_config.py tests/test_recommend.py tests/test_summarize.py tests/test_workflows.py tests/test_acceptance.py`

Expected: failures identify the missing unlimited counter, old 40/6 configuration, one-shot timeout behavior, and old summary request quota.

- [ ] **Step 3: Implement unlimited production attempt accounting**

Add:

```python
class RequestCounter:
    def __init__(self) -> None:
        self._used = 0
        self._lock = Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def consume(self) -> None:
        with self._lock:
            self._used += 1


class RequestBudget(RequestCounter):
    def __init__(self, maximum: int):
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("maximum must be a positive integer")
        super().__init__()
        self._maximum = maximum

    @property
    def remaining(self) -> int:
        return self._maximum - self.used

    def consume(self) -> None:
        with self._lock:
            if self._used >= self._maximum:
                raise RuntimeError("request budget exhausted")
            self._used += 1
```

Change `DeepSeekClient.complete_json()` to call `counter.consume()` inside a three-attempt `call_with_retry()` operation. Retry sanitized transient HTTP/network errors and `_ModelResponseError`; keep 400/401/402/403/404/422 as one attempt. Accumulate usage only for responses carrying complete usage fields.

- [ ] **Step 4: Remove configuration and summary quota checks**

Change `AI_LIMITS["daily_candidates"]` to `100`, remove `max_requests` from `AI_LIMITS`, `AiConfig`, JSON configuration, and the cross-field reserve validation. In `run_summary()`:

```python
used_candidates, _, _ = _today_usage(previous_usage, day)
candidate_limit = max(0, config.ai.daily_candidates - used_candidates)
if candidate_limit == 0:
    return SummaryStats(0, 0, 0, 0, len(state.pending_ai), False)

counter = RequestCounter()
```

Remove every `budget.remaining` early exit. Pass the counter through screening, recommendation, and overview. Recommendation runs before overview. Rework `recommend_one()` to pass the shared counter directly; retry count is owned by `DeepSeekClient`, not a recommendation-only budget wrapper.

- [ ] **Step 5: Update workflow smoke and configuration**

Use `RequestCounter()` in the inline smoke test and update its wording to “one logical DeepSeek smoke operation”. Keep secret boundaries unchanged. Set:

```json
"ai": {
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-v4-flash-vision-exp",
  "daily_candidates": 100,
  "batch_size": 10,
  "max_abstract_chars": 1200,
  "screening_max_tokens": 4096,
  "digest_max_tokens": 8192,
  "recommendation_max_tokens": 512
}
```

- [ ] **Step 6: Verify DeepSeek and summary behavior**

Run: `python -m pytest -q tests/test_ai.py tests/test_config.py tests/test_recommend.py tests/test_summarize.py tests/test_workflows.py tests/test_acceptance.py`

Expected: all tests pass; tests show 100 candidates, no global request ceiling, at most three attempts per operation, recommendation before overview, and accurate request/token reporting.

- [ ] **Step 7: Commit**

```bash
git add src/diamond_feed/ai.py src/diamond_feed/config.py src/diamond_feed/recommend.py src/diamond_feed/summarize.py paper_feed_config.json .github/workflows/summarize.yml tests/test_ai.py tests/test_config.py tests/test_recommend.py tests/test_summarize.py tests/test_workflows.py tests/test_acceptance.py
git commit -m "feat: process one hundred papers with resilient AI calls"
```

---

### Task 5: Retry each Bark message independently

**Files:**
- Modify: `src/diamond_feed/bark.py`
- Modify: `src/diamond_feed/notify.py`
- Modify: `tests/test_bark.py`

**Interfaces:**
- Consumes: shared retry policy.
- Preserves: `send_plan(...) -> tuple[bool, ...]`; tuple length still equals message count, not HTTP attempt count.

- [ ] **Step 1: Write failing Bark retry tests**

```python
def test_bark_retries_first_message_without_suppressing_second():
    transport = SequenceTransport([
        BarkResponse(503, b'{"code":503}'),
        BarkResponse(200, b'{"code":200}'),
        BarkResponse(200, b'{"code":200}'),
    ])
    waits = []

    result = send_plan(
        "bark-secret",
        plan(),
        transport=transport,
        wait=waits.append,
    )

    assert result == (True, True)
    assert len(transport.calls) == 3
    assert waits == [1.0]
```

Add permanent 401 (one attempt), timeout (three attempts), malformed 200 JSON followed by valid JSON, and final-failure secret-redaction cases.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_bark.py`

Expected: transient cases currently make one attempt and fail.

- [ ] **Step 3: Wrap one message delivery in shared retry**

Add `wait` and `policy` keyword arguments to `send_plan`. Make `BarkProtocolError` carry a sanitized `status: int | None` and `retryable: bool`. For each message, build the secret-bearing body once in memory and call `call_with_retry()` around transport plus `_validate_response()`. Retry network errors, retryable HTTP status, and malformed/invalid 2xx response; log only once after the final failure.

- [ ] **Step 4: Verify Bark tests**

Run: `python -m pytest -q tests/test_bark.py tests/test_notification.py`

Expected: all messages retry independently, failures remain nonfatal, and no test output contains the token.

- [ ] **Step 5: Commit**

```bash
git add src/diamond_feed/bark.py src/diamond_feed/notify.py tests/test_bark.py
git commit -m "feat: retry transient Bark delivery failures"
```

---

### Task 6: Retry evaluation-only account requests

**Files:**
- Modify: `src/diamond_feed/evaluate.py`
- Modify: `tests/test_evaluate.py`

**Interfaces:**
- Consumes: shared retry policy.
- Preserves: `read_balance(key, base_url) -> dict[str, Decimal] | None`; balances never reach logs.

- [ ] **Step 1: Write a failing balance retry test**

Patch `urlopen` to raise `TimeoutError` twice and return a valid in-memory response on the third call. Patch the wait function and assert three attempts, waits `[1.0, 2.0]`, and parsed balances. Add a 401 case and assert one attempt and `None`.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_evaluate.py`

Expected: `read_balance` returns `None` after the first transient failure.

- [ ] **Step 3: Apply shared retry without exposing private balance data**

Move the request/JSON parse into a private operation; retry timeout, connection, `URLError`, retryable `HTTPError`, and malformed JSON. Catch the final exception in `read_balance()` and return `None` exactly as before.

- [ ] **Step 4: Verify evaluation tests**

Run: `python -m pytest -q tests/test_evaluate.py`

Expected: retry cases pass; report output remains free of raw balances and credentials.

- [ ] **Step 5: Commit**

```bash
git add src/diamond_feed/evaluate.py tests/test_evaluate.py
git commit -m "feat: retry evaluation account requests"
```

---

### Task 7: Documentation, full verification, and deployment

**Files:**
- Modify: `README.md`
- Verify: all source, tests, workflows, XML outputs, and published URLs.

**Interfaces:**
- Documents the deployed behavior and the confirmed Zotero restart recovery.
- Produces no new runtime API.

- [ ] **Step 1: Update README assertions first**

Extend acceptance tests to require documentation of: 100 papers per day, no DeepSeek daily request ceiling, three-attempt transient retries, approximate 5–20 new broad candidates per day after backlog, and the Zotero restart recovery confirmed during diagnosis.

- [ ] **Step 2: Verify documentation tests fail**

Run: `python -m pytest -q tests/test_acceptance.py`

Expected: README content assertions fail.

- [ ] **Step 3: Update README**

Explain the processing schedule and cost plainly:

```markdown
正常摘要任务每天最多处理 100 篇待筛选候选，不限制当天 DeepSeek 的总请求次数；每 10 篇为一个筛选批次。所有应用层外部 HTTP 操作对超时、连接错误、HTTP 408/425/429/5xx 最多尝试 3 次，永久 4xx 不重试。请求次数和 token 仍写入 ai_usage.json。

如果 Zotero 的订阅数量长时间不变，先确认线上 XML 已更新，再重启 Zotero 并手动刷新。本次 18→23 篇未更新是本地刷新调度停滞，重启后恢复；RSS URL 和 GUID 无需修改。
```

- [ ] **Step 4: Run complete local verification**

Run:

```bash
python -m pytest -q
python -m compileall -q src tests
python -c "from xml.etree import ElementTree as ET; ET.parse('ai_summary_feed.xml'); ET.parse('device_focus_feed.xml'); ET.parse('filtered_feed.xml')"
git diff --check
git status --short
```

Expected: all tests pass, compilation and XML parsing exit zero, no whitespace errors, and only intended files are modified.

- [ ] **Step 5: Review the branch diff for secrets and unrelated changes**

Run:

```bash
git diff origin/main...HEAD --stat
git diff origin/main...HEAD | rg -n "sk-[A-Za-z0-9]|device_key|Authorization: Bearer"
```

Expected: the first command lists only planned files; the second prints no secret values.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md tests/test_acceptance.py
git commit -m "docs: explain reliable feed processing"
```

- [ ] **Step 7: Rebase safely over automated data commits and deploy**

```bash
git fetch origin main
git rebase origin/main
python -m pytest -q
git push origin HEAD:main
```

Expected: non-force push succeeds. Resolve only conflicts in generated data by preserving the newest remote `state.json`, feeds, usage, and failure report; never overwrite newer automated data with stale worktree outputs.

- [ ] **Step 8: Verify GitHub Actions and GitHub Pages**

Trigger the summary workflow once. Confirm tests and summary complete, inspect sanitized counters, and ensure no secret appears in logs. Because the current UTC-day allowance may already be partly consumed, a no-op or partial run is acceptable if counters match the persisted daily usage.

Fetch `https://llxyyds666.github.io/diamond-paper-feed/device_focus_feed.xml` with a cache-busting query and verify HTTP 200, `Content-Type: application/xml`, unchanged stable GUIDs, and valid XML. Do not resubscribe or migrate Zotero GUIDs: the existing subscription refreshed successfully after restarting Zotero.
