from __future__ import annotations

from http.client import IncompleteRead

from scripts.phase2_validation import fetch_json


class FakeResponse:
    status = 200

    def __init__(self, body: bytes | None = None, error: Exception | None = None):
        self.body = body
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self) -> bytes:
        if self.error is not None:
            raise self.error
        return self.body or b""


def test_fetch_json_records_a_truncated_response(monkeypatch):
    response = FakeResponse(error=IncompleteRead(b'{"batch":', 20))
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    payload, latency_ms, status, error = fetch_json("http://validation/status")

    assert payload == {}
    assert latency_ms >= 0
    assert status == 0
    assert error is not None
    assert error.startswith("IncompleteRead:")


def test_fetch_json_rejects_a_non_object_json_response(monkeypatch):
    response = FakeResponse(body=b"null")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    payload, _latency_ms, status, error = fetch_json("http://validation/status")

    assert payload == {}
    assert status == 0
    assert error == "ValueError: JSON response must be an object"
