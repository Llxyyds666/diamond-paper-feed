import pytest

from diamond_feed.retry import (
    DEFAULT_RETRY_POLICY,
    RetryPolicy,
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
            wait=lambda seconds: pytest.fail(f"must not wait: {seconds}"),
        )
    assert len(attempts) == 1


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 599])
def test_retryable_http_statuses(status):
    assert retryable_http_status(status) is True


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 422])
def test_permanent_http_statuses(status):
    assert retryable_http_status(status) is False


@pytest.mark.parametrize(
    ("attempts", "delays"),
    [(0, ()), (2, ()), (2, (-1.0,))],
)
def test_retry_policy_rejects_invalid_shapes(attempts, delays):
    with pytest.raises(ValueError, match="invalid retry policy"):
        RetryPolicy(attempts, delays)
