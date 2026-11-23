"""모델 게이트웨이 계약 테스트. Ollama 는 respx 로 가짜 응답을 준다 (실제 모델·네트워크 없음).

Django 의 chat/fastapi_client.py 가 이 계약을 그대로 읽는다:
    POST /embed     {"inputs": [...]} -> {"embeddings": [[...], ...]}
    POST /generate  {"prompt": str}   -> {"text": str, "truncated": bool}
Ollama 실패(5xx, 연결 끊김)는 503 이어야 Django 가 인프라 실패로 보고 재시도한다.
"""

import logging

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

OLLAMA = "http://ollama.test:11434"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA)
    monkeypatch.setenv("GENERATE_MODEL", "gen-model")
    monkeypatch.setenv("EMBED_MODEL", "embed-model")
    from config import get_settings
    from main import app

    get_settings.cache_clear()
    with respx.mock(base_url=OLLAMA, assert_all_called=False) as ollama, TestClient(app) as http:
        http.ollama = ollama
        yield http
    get_settings.cache_clear()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_embed_forwards_inputs_and_returns_vectors(client):
    route = client.ollama.post("/api/embed").respond(json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    resp = client.post("/embed", json={"inputs": ["a", "b"]})

    assert resp.status_code == 200
    assert resp.json() == {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    sent = route.calls.last.request
    assert httpx.Response(200, content=sent.content).json() == {"model": "embed-model", "input": ["a", "b"]}


def test_embed_rejects_empty_inputs(client):
    assert client.post("/embed", json={"inputs": []}).status_code == 422


@pytest.mark.parametrize(("done_reason", "truncated"), [("stop", False), ("length", True), (None, False)])
def test_generate_reports_truncation(client, done_reason, truncated):
    body = {"response": "답 [12]", "done": True}
    if done_reason is not None:
        body["done_reason"] = done_reason
    client.ollama.post("/api/generate").respond(json=body)

    resp = client.post("/generate", json={"prompt": "질문"})

    assert resp.status_code == 200
    assert resp.json() == {"text": "답 [12]", "truncated": truncated}


def test_generate_sends_fixed_limits_and_reasoning_off(client):
    route = client.ollama.post("/api/generate").respond(json={"response": "x", "done": True})

    client.post("/generate", json={"prompt": "질문"})

    sent = httpx.Response(200, content=route.calls.last.request.content).json()
    assert sent["model"] == "gen-model"
    assert sent["stream"] is False
    assert sent["think"] is False
    assert sent["options"] == {"num_predict": 256, "num_ctx": 4096}


def test_generate_logs_readme_metrics(client, caplog):
    # Ollama 시간은 ns. 프롬프트 700토큰/10초 = 70 tok/s, 출력 90토큰/20초 = 4.5 tok/s (README 표와 같은 계산)
    client.ollama.post("/api/generate").respond(json={
        "response": "x", "done": True, "done_reason": "stop",
        "prompt_eval_count": 700, "prompt_eval_duration": 10_000_000_000,
        "eval_count": 90, "eval_duration": 20_000_000_000,
        "load_duration": 500_000_000, "total_duration": 30_500_000_000,
    })

    with caplog.at_level(logging.INFO, logger="ollama"):
        client.post("/generate", json={"prompt": "질문"})

    line = next(r.getMessage() for r in caplog.records if "metric=generate" in r.getMessage())
    for part in ("prompt_tokens=700", "prompt_tok_s=70.0", "output_tokens=90", "output_tok_s=4.50",
                 "ttft_s=10.50", "load_s=0.50", "total_s=30.50", "done_reason=stop"):
        assert part in line, (part, line)


def test_generate_metrics_survive_missing_fields(client, caplog):
    client.ollama.post("/api/generate").respond(json={"response": "x", "done": True})

    with caplog.at_level(logging.INFO, logger="ollama"):
        assert client.post("/generate", json={"prompt": "질문"}).status_code == 200

    assert any("metric=generate" in r.getMessage() and "output_tok_s=0.00" in r.getMessage() for r in caplog.records)


def test_embed_logs_input_count_and_time(client, caplog):
    client.ollama.post("/api/embed").respond(json={"embeddings": [[0.1]], "total_duration": 650_000_000})

    with caplog.at_level(logging.INFO, logger="ollama"):
        client.post("/embed", json={"inputs": ["질문"]})

    assert any("metric=embed inputs=1 total_s=0.65" in r.getMessage() for r in caplog.records)


def test_ollama_server_error_becomes_503(client):
    client.ollama.post("/api/generate").respond(status_code=500)

    resp = client.post("/generate", json={"prompt": "질문"})

    assert resp.status_code == 503
    assert resp.json()["detail"].startswith("ollama:")


def test_ollama_connection_error_becomes_503(client):
    client.ollama.post("/api/embed").mock(side_effect=httpx.ConnectError("refused"))

    assert client.post("/embed", json={"inputs": ["a"]}).status_code == 503


def test_health_probe_is_not_access_logged(caplog):
    import main  # noqa: F401  필터 등록

    access = logging.getLogger("uvicorn.access")
    fmt = '%s - "%s %s HTTP/%s" %d'
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info(fmt, "10.0.0.1:5", "GET", "/health", "1.1", 200)
        access.info(fmt, "10.0.0.1:5", "POST", "/generate", "1.1", 200)

    assert [r.getMessage() for r in caplog.records] == ['10.0.0.1:5 - "POST /generate HTTP/1.1" 200']
