"""게이트웨이 응답을 어떻게 분류하는지. HTTP 는 httpx.MockTransport 로 흉내 낸다 (네트워크·DB 없음)."""

import json

import httpx
from django.test import SimpleTestCase, override_settings

from chat.fastapi_client import FastAPIClient, FastAPIError, GenerateResult, InfraError, with_infra_retry
from chat.models import EMBEDDING_DIMENSIONS


def client_returning(*responses):
    """요청마다 다음 응답(httpx.Response 또는 예외)을 돌려주는 클라이언트와, 받은 요청 목록."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return FastAPIClient("http://ai.test", timeout=1, transport=httpx.MockTransport(handler)), seen


class GenerateTests(SimpleTestCase):
    def test_returns_text_and_truncation(self):
        client, seen = client_returning(httpx.Response(200, json={"text": "답", "truncated": True}))
        self.assertEqual(client.generate("질문"), GenerateResult(text="답", truncated=True))
        self.assertEqual((seen[0].url.path, json.loads(seen[0].content)), ("/generate", {"prompt": "질문"}))

    def test_missing_truncated_is_retried_not_assumed_complete(self):
        # 구버전 게이트웨이(배포 중): 잘림을 모르니 답을 쓰지 않고, 새 버전이 뜰 때까지 재시도한다.
        client, _ = client_returning(httpx.Response(200, json={"text": "답"}))
        with self.assertRaises(InfraError):
            client.generate("질문")

    def test_missing_text_is_rejected(self):
        client, _ = client_returning(httpx.Response(200, json={"truncated": False}))
        with self.assertRaises(FastAPIError):
            client.generate("질문")


class EmbedTests(SimpleTestCase):
    def test_returns_one_vector_per_input(self):
        vector = [0.0] * EMBEDDING_DIMENSIONS
        client, seen = client_returning(httpx.Response(200, json={"embeddings": [vector, vector]}))
        self.assertEqual(client.embed(["a", "b"]), [vector, vector])
        self.assertEqual(json.loads(seen[0].content), {"inputs": ["a", "b"]})

    def test_wrong_dimension_is_rejected(self):
        client, _ = client_returning(httpx.Response(200, json={"embeddings": [[0.0, 1.0]]}))
        with self.assertRaises(FastAPIError):
            client.embed(["a"])

    def test_count_mismatch_is_rejected(self):
        client, _ = client_returning(httpx.Response(200, json={"embeddings": []}))
        with self.assertRaises(FastAPIError):
            client.embed(["a"])


class ErrorClassificationTests(SimpleTestCase):
    """인프라 실패만 InfraError(재시도, 유저 실패 아님). 나머지 거절은 FastAPIError."""

    def assert_infra(self, response_or_error):
        client, _ = client_returning(response_or_error)
        with self.assertRaises(InfraError):
            client.generate("질문")

    def test_server_errors_and_rate_limit_are_infra(self):
        for status in (500, 502, 503, 429):
            with self.subTest(status=status):
                self.assert_infra(httpx.Response(status, json={"detail": "ollama: ConnectError"}))

    def test_connection_errors_are_infra(self):
        self.assert_infra(httpx.ConnectError("refused"))
        self.assert_infra(httpx.ReadTimeout("slow"))

    def test_client_errors_are_not_retried(self):
        client, _ = client_returning(httpx.Response(422, json={"detail": "bad"}))
        with self.assertRaises(FastAPIError) as ctx:
            client.generate("질문")
        self.assertNotIsInstance(ctx.exception, InfraError)

    def test_non_json_response_is_rejected(self):
        client, _ = client_returning(httpx.Response(200, text="<html>"))
        with self.assertRaises(FastAPIError):
            client.generate("질문")


@override_settings(FASTAPI_INFRA_RETRY_DELAYS=(5, 10))
class InfraRetryTests(SimpleTestCase):
    def test_infra_error_is_retried_after_each_delay(self):
        calls, slept = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise InfraError("503")
            return "ok"

        self.assertEqual(with_infra_retry(flaky, sleep=slept.append), "ok")
        self.assertEqual(slept, [5, 10])

    def test_last_infra_error_is_raised_after_all_delays(self):
        def down():
            raise InfraError("503")

        slept = []
        with self.assertRaises(InfraError):
            with_infra_retry(down, sleep=slept.append)
        self.assertEqual(slept, [5, 10])

    def test_gateway_rejection_is_not_retried(self):
        slept = []

        def rejected():
            raise FastAPIError("422")

        with self.assertRaises(FastAPIError):
            with_infra_retry(rejected, sleep=slept.append)
        self.assertEqual(slept, [])


class ErrorCodeTests(SimpleTestCase):
    """에러 코드: ai 까지 못 간 것(ai_*), ai 가 알려준 것(body 의 code), 그 밖(http_<상태>)을 나눈다."""

    def error_for(self, response_or_error):
        client, _ = client_returning(response_or_error)
        with self.assertRaises(FastAPIError) as ctx:
            client.embed(["a"])
        return ctx.exception

    def test_transport_failures_to_ai_are_split(self):
        cases = [
            (httpx.ConnectTimeout("t"), "ai_connect_timeout"),
            (httpx.ConnectError("refused"), "ai_connect_error"),
            (httpx.ReadTimeout("slow"), "ai_timeout"),
            (httpx.RemoteProtocolError("closed"), "ai_disconnected"),
        ]
        for error, code in cases:
            with self.subTest(code=code):
                exc = self.error_for(error)
                self.assertIsInstance(exc, InfraError)
                self.assertEqual((exc.code, exc.status), (code, None))

    def test_ai_reported_code_is_kept_with_status(self):
        for status, code in ((503, "ollama_unreachable"), (504, "ollama_timeout"), (502, "ollama_http_404")):
            with self.subTest(code=code):
                exc = self.error_for(httpx.Response(status, json={"detail": "ollama: x", "code": code}))
                self.assertIsInstance(exc, InfraError)
                self.assertEqual((exc.code, exc.status), (code, status))
                self.assertIn(f"{code} (HTTP {status})", str(exc))

    def test_without_body_code_falls_back_to_http_status(self):
        exc = self.error_for(httpx.Response(500, text="Internal Server Error"))
        self.assertEqual((exc.code, exc.status), ("http_500", 500))
        exc = self.error_for(httpx.Response(429, json={"detail": "busy"}))
        self.assertEqual(exc.code, "http_429")

    def test_client_error_and_bad_response_codes(self):
        exc = self.error_for(httpx.Response(422, json={"detail": "bad"}))
        self.assertNotIsInstance(exc, InfraError)
        self.assertEqual((exc.code, exc.status), ("http_422", 422))
        self.assertEqual(self.error_for(httpx.Response(200, text="<html>")).code, "bad_response")
        self.assertEqual(self.error_for(httpx.Response(200, json={"embeddings": []})).code, "bad_response")


class CallLogTests(SimpleTestCase):
    def test_request_and_result_lines_share_an_id_sent_to_ai(self):
        vector = [0.0] * EMBEDDING_DIMENSIONS
        client, seen = client_returning(httpx.Response(200, json={"embeddings": [vector]}))
        with self.assertLogs("chat.fastapi_client", level="INFO") as logs:
            client.embed(["a"])
        rid = seen[0].headers["X-Request-Id"]
        self.assertEqual(len(logs.output), 2)
        self.assertIn(f"fastapi req id={rid} POST /embed", logs.output[0])
        self.assertIn(f"fastapi res id={rid} POST /embed status=200 code=ok", logs.output[1])

    def test_failure_result_is_warning_with_code(self):
        client, _ = client_returning(httpx.Response(503, json={"detail": "x", "code": "ollama_unreachable"}))
        with self.assertLogs("chat.fastapi_client", level="INFO") as logs, self.assertRaises(InfraError):
            client.embed(["a"])
        self.assertTrue(logs.output[-1].startswith("WARNING"))
        self.assertIn("status=503 code=ollama_unreachable", logs.output[-1])


class MetricsTests(SimpleTestCase):
    def test_generate_and_embed_keep_ai_metrics_when_present(self):
        vector = [0.0] * EMBEDDING_DIMENSIONS
        client, _ = client_returning(
            httpx.Response(200, json={"embeddings": [vector], "metrics": {"total_s": 0.4}}),
            httpx.Response(200, json={"text": "답", "truncated": False, "metrics": {"output_tokens": 61}}),
        )
        client.embed(["a"])
        self.assertEqual(client.last_embed_metrics, {"total_s": 0.4})
        self.assertEqual(client.generate("q").metrics, {"output_tokens": 61})

    def test_old_gateway_without_metrics_still_works(self):
        client, _ = client_returning(httpx.Response(200, json={"text": "답", "truncated": False}))
        self.assertEqual(client.generate("q").metrics, {})
