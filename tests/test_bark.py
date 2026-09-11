import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from diamond_feed.bark import BARK_ICON_URL, BarkResponse, send_plan
from diamond_feed.notification import (
    NotificationMessage,
    NotificationPlan,
    write_notification_plan,
)
from diamond_feed import bark, notify


class BarkTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def plan():
    return NotificationPlan(
        (
            NotificationMessage(
                "金刚石文献日报", "统计", "https://example.test/ai_summary.html"
            ),
            NotificationMessage("今日推荐", "论文与理由", "https://doi.org/10.1000/x"),
        )
    )


def success_response():
    return BarkResponse(200, b'{"code":200,"message":"success"}')


def test_bark_uses_official_json_endpoint_and_keeps_token_out_of_url():
    transport = BarkTransport([success_response(), success_response()])

    result = send_plan("bark-secret", plan(), transport=transport)

    assert result == (True, True)
    assert len(transport.calls) == 2
    for index, call in enumerate(transport.calls):
        url, headers, body, timeout = call
        payload = json.loads(body)
        assert url == "https://api.day.app/push"
        assert "bark-secret" not in url
        assert headers["Content-Type"] == "application/json"
        assert payload == {
            "device_key": "bark-secret",
            "title": plan().messages[index].title,
            "body": plan().messages[index].body,
            "group": "diamond-paper-feed",
            "icon": BARK_ICON_URL,
            "url": plan().messages[index].url,
        }
        assert payload["icon"] == (
            "https://llxyyds666.github.io/diamond-paper-feed/"
            "assets/diamond-bark-icon.png"
        )
        assert timeout == 10.0


def test_bark_omits_url_when_message_has_none_and_propagates_timeout():
    no_url_plan = NotificationPlan(
        (
            NotificationMessage("one", "first", None),
            NotificationMessage("two", "second", None),
        )
    )
    transport = BarkTransport([success_response(), success_response()])

    assert send_plan(
        "bark-secret", no_url_plan, transport=transport, timeout_seconds=2.5
    ) == (True, True)

    assert all("url" not in json.loads(call[2]) for call in transport.calls)
    assert [call[3] for call in transport.calls] == [2.5, 2.5]


def test_bark_retries_first_message_without_suppressing_second():
    transport = BarkTransport([
        BarkResponse(503, b'{"code":503}'),
        success_response(),
        success_response(),
    ])
    waits = []

    result = send_plan(
        "bark-secret", plan(), transport=transport, wait=waits.append
    )

    assert result == (True, True)
    assert len(transport.calls) == 3
    assert waits == [1.0]


def test_first_bark_failure_does_not_suppress_second_and_logs_no_secret(capsys):
    transport = BarkTransport(
        [
            RuntimeError("bark-secret response body"),
            BarkResponse(500, b"bark-secret response body"),
            BarkResponse(500, b"bark-secret response body"),
            BarkResponse(500, b"bark-secret response body"),
        ]
    )

    result = send_plan(
        "bark-secret", plan(), transport=transport, wait=lambda _: None
    )

    captured = capsys.readouterr()
    assert result == (False, False)
    assert len(transport.calls) == 4
    assert "bark-secret" not in captured.err
    assert "response body" not in captured.err
    assert "Bark notification 1 failed: RuntimeError status=none" in captured.err
    assert "Bark notification 2 failed:" in captured.err
    assert "status=500" in captured.err


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        (BarkResponse(200, b"not-json"), True),
        (BarkResponse(200, b"[]"), True),
        (BarkResponse(200, b'{"code":201}'), True),
        (BarkResponse(200, b'{"code":true}'), True),
        (BarkResponse(204, b""), True),
        (BarkResponse(299, b'{"code":"200"}'), True),
        (BarkResponse(300, b'{"code":200}'), False),
    ],
)
def test_bark_requires_2xx_and_json_object_with_integer_200(
    response, retryable, capsys
):
    attempts = 3 if retryable else 1
    transport = BarkTransport([response] * attempts + [success_response()])
    waits = []

    result = send_plan(
        "bark-secret", plan(), transport=transport, wait=waits.append
    )

    captured = capsys.readouterr()
    assert result == (False, True)
    assert len(transport.calls) == attempts + 1
    assert waits == ([1.0, 2.0] if retryable else [])
    assert "bark-secret" not in captured.err
    assert "Bark notification 1 failed:" in captured.err


@pytest.mark.parametrize(
    "body",
    [
        b'{"code":200,"detail":NaN}',
        b'{"code":500,"code":200}',
    ],
)
def test_bark_rejects_nonstandard_constants_and_duplicate_keys_without_leaking(
    body, capsys
):
    transport = BarkTransport([BarkResponse(200, body)] * 6)

    result = send_plan(
        "bark-secret", plan(), transport=transport, wait=lambda _: None
    )

    captured = capsys.readouterr()
    assert result == (False, False)
    assert len(transport.calls) == 6
    assert captured.err.count("Bark notification") == 2
    assert "bark-secret" not in captured.err
    assert body.decode("ascii") not in captured.err


def test_bark_does_not_retry_permanent_401_response():
    transport = BarkTransport([
        BarkResponse(401, b'{"code":401}'),
        success_response(),
    ])
    waits = []

    result = send_plan(
        "bark-secret", plan(), transport=transport, wait=waits.append
    )

    assert result == (False, True)
    assert len(transport.calls) == 2
    assert waits == []


def test_bark_retries_timeout_for_one_message_only():
    transport = BarkTransport([
        TimeoutError("temporary"),
        TimeoutError("temporary"),
        success_response(),
        success_response(),
    ])
    waits = []

    result = send_plan(
        "bark-secret", plan(), transport=transport, wait=waits.append
    )

    assert result == (True, True)
    assert len(transport.calls) == 4
    assert waits == [1.0, 2.0]


def test_default_transport_rejects_redirect_without_contacting_target(
    monkeypatch, capsys
):
    requests = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            requests.append(("POST", self.path, self.rfile.read(length)))
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.end_headers()

        def do_GET(self):
            requests.append(("GET", self.path, b""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"code":200}')

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        monkeypatch.setattr(bark, "BARK_URL", f"http://{host}:{port}/push")

        result = send_plan("bark-secret", plan(), timeout_seconds=2.5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    captured = capsys.readouterr()
    assert result == (False, False)
    assert [(method, path) for method, path, _ in requests] == [
        ("POST", "/push"),
        ("POST", "/push"),
    ]
    for _, _, body in requests:
        assert json.loads(body)["device_key"] == "bark-secret"
    assert captured.err.count("status=302") == 2
    assert "bark-secret" not in captured.err
    assert "redirect-target" not in captured.err


def test_notify_cli_reads_secret_from_environment_and_delivery_failure_is_nonfatal(
    tmp_path, monkeypatch
):
    path = tmp_path / "notification.json"
    write_notification_plan(path, plan())
    seen = {}
    monkeypatch.setenv("BARK_TOKEN", "bark-secret")
    monkeypatch.setattr(
        notify,
        "send_plan",
        lambda token, loaded: seen.update(token=token, loaded=loaded)
        or (False, False),
    )

    assert notify.main(["--plan", str(path)]) == 0
    assert seen == {"token": "bark-secret", "loaded": plan()}


def test_notify_cli_passes_configured_timeout(tmp_path, monkeypatch):
    path = tmp_path / "notification.json"
    write_notification_plan(path, plan())
    seen = {}
    monkeypatch.setenv("BARK_TOKEN", "bark-secret")
    monkeypatch.setattr(
        notify,
        "send_plan",
        lambda token, loaded, *, timeout_seconds: seen.update(
            token=token, loaded=loaded, timeout=timeout_seconds
        )
        or (True, True),
    )

    assert notify.main(
        ["--plan", str(path), "--timeout-seconds", "3.25"]
    ) == 0
    assert seen == {"token": "bark-secret", "loaded": plan(), "timeout": 3.25}


@pytest.mark.parametrize("token", [None, "", "   "])
def test_notify_cli_missing_or_blank_token_is_sanitized_and_nonfatal(
    tmp_path, monkeypatch, capsys, token
):
    path = tmp_path / "notification.json"
    write_notification_plan(path, plan())
    if token is None:
        monkeypatch.delenv("BARK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("BARK_TOKEN", token)
    monkeypatch.setattr(
        notify,
        "send_plan",
        lambda *args, **kwargs: pytest.fail("delivery must not be attempted"),
    )

    assert notify.main(["--plan", str(path)]) == 0

    captured = capsys.readouterr()
    assert "BARK_TOKEN" in captured.err
    assert token is None or token not in captured.err or not token.strip()


@pytest.mark.parametrize("contents", [None, "not-json", '{}'])
def test_notify_cli_missing_or_invalid_plan_is_sanitized_and_nonfatal(
    tmp_path, monkeypatch, capsys, contents
):
    path = tmp_path / "contains-bark-secret.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("BARK_TOKEN", "bark-secret")
    monkeypatch.setattr(
        notify,
        "send_plan",
        lambda *args, **kwargs: pytest.fail("delivery must not be attempted"),
    )

    assert notify.main(["--plan", str(path)]) == 0

    captured = capsys.readouterr()
    assert "notification plan" in captured.err.lower()
    assert "bark-secret" not in captured.err
    assert str(path) not in captured.err


@pytest.mark.parametrize("timeout", ["zero", "0", "-1", "nan", "inf"])
def test_notify_cli_invalid_timeout_is_sanitized_and_nonfatal(
    tmp_path, monkeypatch, capsys, timeout
):
    path = tmp_path / "notification.json"
    write_notification_plan(path, plan())
    monkeypatch.setenv("BARK_TOKEN", "bark-secret")
    monkeypatch.setattr(
        notify,
        "send_plan",
        lambda *args, **kwargs: pytest.fail("delivery must not be attempted"),
    )

    assert notify.main(
        ["--plan", str(path), "--timeout-seconds", timeout]
    ) == 0

    captured = capsys.readouterr()
    assert "configuration" in captured.err.lower()
    assert "bark-secret" not in captured.err
