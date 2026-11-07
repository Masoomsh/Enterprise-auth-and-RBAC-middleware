from pathlib import Path

import httpx
import pytest

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest


def test_latest_answer_remains_visible_without_showing_older_turns(monkeypatch):
    def fake_request(method, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "id": "test-request",
                "model": "test-model",
                "choices": [{
                    "message": {"content": "The latest answer is visible."},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 4},
            },
            headers={
                "x-cache": "MISS",
                "x-request-id": "test-request",
                "x-routed-model": "test-model",
                "x-query-complexity": "simple",
                "x-experiment-arm": "control",
            },
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    app = AppTest.from_file(app_path, default_timeout=30).run()
    assert not app.exception

    app.session_state["conversation"] = [
        {"role": "user", "content": "Older question must not be shown."},
        {"role": "assistant", "content": "Older answer must not be shown."},
    ]
    app.run()
    app.chat_input[0].set_value("Latest question").run()

    assert not app.exception
    displayed_text = "\n".join(item.value for item in app.markdown)
    assert "Latest question" in displayed_text
    assert "The latest answer is visible." in displayed_text
    assert "Older question must not be shown." not in displayed_text
    assert "Older answer must not be shown." not in displayed_text
