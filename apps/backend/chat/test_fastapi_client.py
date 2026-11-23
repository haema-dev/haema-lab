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
