"""qa tests. Need PostgreSQL with pgvector (POSTGRES_HOST set); skipped otherwise.

FastAPI is replaced by httpx.MockTransport and Redis calls are patched, so
these tests check the DB reads/writes and the step order, not the models.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import timedelta
from unittest import mock, skipUnless

import httpx
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from qa import repository
from qa.fastapi_client import FastAPIClient, FastAPIError, InfraError, RetryCounter, with_infra_retry
from qa.management.commands.index_documents import render_events_json, split_into_chunks
from qa.models import Answer, AnswerCitation, DocumentChunk, Job, Message, SourceDocument
from qa.worker import process_job

POSTGRES = connection.vendor == "postgresql"
DIM = 1024
USER = 7


def vec(*hot: int) -> list[float]:
    values = [0.0] * DIM
    for index in hot:
        values[index] = 1.0
    return values


class FakeFastAPI:
    """Scripted gateway. ``routes[path]`` is a dict, an int status, or a callable(payload)."""

    def __init__(self, **routes) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        route = self.routes[path.strip("/")]
        if callable(route):
            route = route(json.loads(request.content))
        if isinstance(route, int):
            return httpx.Response(route, text="upstream error")
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)

    def client(self) -> FastAPIClient:
        return FastAPIClient("http://fastapi", transport=httpx.MockTransport(self.handler))


def add_chunk(text: str, vector: list[float], external_id: str = "doc") -> DocumentChunk:
    document, _ = SourceDocument.objects.get_or_create(
        external_id=external_id, defaults={"title": external_id, "content_sha256": "0" * 64}
    )
    index = DocumentChunk.objects.filter(document=document).count()
    return DocumentChunk.objects.create(
        document=document, chunk_index=index, content=text, embedding=vector, embedding_model="emb"
    )


_patch_redis = [
    mock.patch("qa.worker.gpu_slot", contextlib.nullcontext),
    mock.patch("qa.worker.notify_job_done", mock.Mock()),
]


def no_redis(cls):
    for patcher in _patch_redis:
        cls = patcher(cls)
    return cls


# ---------------------------------------------------------------------------


class NormalizeTests(TestCase):
    def test_whitespace_case_and_width_do_not_change_the_hash(self):
        self.assertEqual(repository.question_hash("  금리  인상 RATE? "), repository.question_hash("금리 인상 rate?"))
        self.assertEqual(repository.question_hash("ＡＢＣ"), repository.question_hash("abc"))
        self.assertNotEqual(repository.question_hash("금리 인상"), repository.question_hash("금리 인하"))


class SplitTests(TestCase):
    def test_paragraphs_are_packed_and_long_ones_cut(self):
        chunks = split_into_chunks("a" * 5 + "\n\n" + "b" * 5 + "\n\n" + "c" * 25, max_chars=12)
        self.assertEqual(chunks, ["aaaaa\n\nbbbbb", "c" * 12, "c" * 12, "c"])
        self.assertTrue(all(len(c) <= 12 for c in chunks))


class RenderEventsJsonTests(TestCase):
    def test_event_numbers_are_written_the_way_verify_reads_them(self):
        from core.verify import extract_facts

        raw = json.dumps({
            "record_type": "official_events",
            "events": [{
                "event_date": "2026-08-27", "title": "기준금리 인상", "summary": "결정 후 수준은 3.00%입니다.",
                "source_ids": ["s1"],
                "decision": {"policy_instrument": "한국은행 기준금리", "change_basis_points": 25,
                             "previous_rate_percent": 2.75, "target_rate_percent": 3.0, "action_ko": "인상"},
            }, {
                "event_date": "2025-10-29", "title": "FOMC", "summary": "3.75~4.00%",
                "decision": {"policy_instrument": "연방기금금리 목표 범위", "change_basis_points": -25,
                             "target_range_lower_percent": 3.75, "target_range_upper_percent": 4.0},
            }],
            "sources": [{"source_id": "s1", "institution": "한국은행", "url": "https://example.test/" + "x" * 150}],
        })
        text = render_events_json(raw)
        first, second = text.split("\n\n")
        self.assertIn("출처: 한국은행", first)
        self.assertNotIn("https://", text)  # URL is prompt cost, not evidence
        self.assertLessEqual({"2.75%", "3.00%", "25bp", "일:2026-08-27"} - extract_facts(first), set())
        self.assertLessEqual({"3.75%", "4.00%", "25bp"} - extract_facts(second), set())

    def test_other_json_is_rejected(self):
        with self.assertRaises(ValueError):
            render_events_json('{"record_type": "prices"}')


class FastAPIClientTests(TestCase):
    def _client(self, status: int, text: str = "", json_body=None) -> FastAPIClient:
        def handler(request):
            if json_body is not None:
                return httpx.Response(status, json=json_body)
            return httpx.Response(status, text=text)

        return FastAPIClient("http://fastapi", transport=httpx.MockTransport(handler))

    def test_infra_errors(self):
        for status, text in [(503, "down"), (429, "quota"), (200, "vk::Queue::submit: ErrorDeviceLost")]:
            with self.subTest(status=status), self.assertRaises(InfraError):
                self._client(status, text).generate("q", [])

    def test_gateway_retryable_flag_decides(self):
        """apps/ai answers 503 not_configured and 502 bad_output with retryable=false."""
        for status, retryable, expected in [(503, False, FastAPIError), (502, False, FastAPIError), (503, True, InfraError)]:
            body = {"error": {"kind": "x", "reason": "y", "retryable": retryable}}
            with self.subTest(status=status, retryable=retryable), self.assertRaises(FastAPIError) as caught:
                self._client(status, json_body=body).generate("q", [])
            self.assertIs(type(caught.exception), expected)

    def test_embed_sends_inputs_and_checks_dimensions(self):
        sent = {}

        def handler(request):
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={"model": "e", "embeddings": [[0.1] * 3], "dimensions": 3})

        client = FastAPIClient("http://fastapi", transport=httpx.MockTransport(handler))
        with self.assertRaises(FastAPIError):
            client.embed(["q"], kind="query")
        self.assertEqual(sent, {"kind": "query", "inputs": ["q"]})  # the gateway's EmbedRequest field

    def test_connection_error_is_infra(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        client = FastAPIClient("http://fastapi", transport=httpx.MockTransport(handler))
        with self.assertRaises(InfraError):
            client.embed(["q"], kind="query")

    def test_client_error_is_not_infra(self):
        with self.assertRaises(FastAPIError) as caught:
            self._client(422, "bad").generate("q", [])
        self.assertNotIsInstance(caught.exception, InfraError)

    def test_truncated_generation(self):
        body = {"model": "m", "text": "x", "done_reason": "length", "truncated": True}
        self.assertTrue(self._client(200, json_body=body).generate("q", []).truncated)

    def test_truncated_fallback_uses_gemini_finish_reason(self):
        body = {"model": "g", "text": "x", "finish_reason": "MAX_TOKENS", "truncated": True}
        fallback = self._client(200, json_body=body).fallback("q", [])
        self.assertTrue(fallback.truncated)
        self.assertEqual(fallback.done_reason, "MAX_TOKENS")

    def test_retry_only_infra(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise InfraError("503")
            return "ok"

        counter = RetryCounter()
        self.assertEqual(with_infra_retry(flaky, counter=counter, delays=[0, 0, 0], sleep=lambda _: None), "ok")
        self.assertEqual(counter.count, 2)

        def broken():
            raise FastAPIError("422")

        with self.assertRaises(FastAPIError):
            with_infra_retry(broken, delays=[0, 0], sleep=lambda _: None)

    def test_retry_waits_at_least_retry_after(self):
        attempts, waits = [], []

        def limited():
            attempts.append(1)
            if len(attempts) == 1:
                raise InfraError("429", retry_after=30)
            return "ok"

        self.assertEqual(with_infra_retry(limited, delays=[5], sleep=waits.append), "ok")
        self.assertEqual(waits, [30])


# ---------------------------------------------------------------------------


@skipUnless(POSTGRES, "needs PostgreSQL + pgvector")
@mock.patch("qa.views.enqueue_job")
class QuestionApiTests(TestCase):
    def post(self, question, user=USER):
        return self.client.post(
            "/api/qa/questions",
            data=json.dumps({"question": question}),
            content_type="application/json",
            headers={"X-User-Id": str(user)},
        )

    def test_creates_message_and_job_and_enqueues_once(self, enqueue):
        response = self.post("기준금리는?")
        self.assertEqual(response.status_code, 202)
        job = Job.objects.get(id=response.json()["job_id"])
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(job.user_message.content, "기준금리는?")
        self.assertEqual(job.user_message.role, Message.Role.USER)
        enqueue.assert_called_once_with(job.id)

    def test_duplicate_returns_existing_job_without_writes(self, enqueue):
        first = self.post("기준금리는?").json()["job_id"]
        second = self.post("  기준금리는? ")
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json(), {"job_id": first, "duplicate": True})
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Job.objects.count(), 1)
        enqueue.assert_called_once()

    def test_other_user_or_finished_job_is_not_a_duplicate(self, enqueue):
        first = self.post("기준금리는?").json()["job_id"]
        self.assertNotEqual(self.post("기준금리는?", user=USER + 1).json()["job_id"], first)
        Job.objects.filter(id=first).update(status=Job.Status.FAILED, failure_type=Job.FailureType.INFRA)
        self.assertNotEqual(self.post("기준금리는?").json()["job_id"], first)

    def test_too_long_question_touches_nothing(self, enqueue):
        response = self.post("가" * 301)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Job.objects.count(), 0)
        enqueue.assert_not_called()
        self.assertEqual(self.post("가" * 300).status_code, 202)

    def test_requires_user_header(self, enqueue):
        response = self.client.post("/api/qa/questions", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_enqueue_failure_frees_the_question(self, enqueue):
        import redis

        enqueue.side_effect = redis.ConnectionError("down")
        response = self.post("기준금리는?")
        self.assertEqual(response.status_code, 503)
        job = Job.objects.get()
        self.assertEqual((job.status, job.failure_type), (Job.Status.FAILED, Job.FailureType.INFRA))
        enqueue.side_effect = None
        self.assertEqual(self.post("기준금리는?").status_code, 202)

    def test_poll_is_owner_only(self, enqueue):
        job_id = self.post("기준금리는?").json()["job_id"]
        own = self.client.get(f"/api/qa/jobs/{job_id}", headers={"X-User-Id": str(USER)})
        self.assertEqual(own.json()["status"], "queued")
        other = self.client.get(f"/api/qa/jobs/{job_id}", headers={"X-User-Id": str(USER + 1)})
        self.assertEqual(other.status_code, 404)


@skipUnless(POSTGRES, "needs PostgreSQL + pgvector")
class ActiveJobIndexTests(TransactionTestCase):
    def test_db_rejects_second_active_job_for_same_question(self):
        qhash = repository.question_hash("q")
        for _ in range(2):
            message = Message.objects.create(user_id=USER, role="user", content="q")
            if Job.objects.exists():
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Job.objects.create(user_id=USER, question_hash=qhash, user_message=message)
            else:
                Job.objects.create(user_id=USER, question_hash=qhash, user_message=message)

    def test_question_longer_than_300_is_rejected_by_db(self):
        with self.assertRaises(IntegrityError):
            Message.objects.create(user_id=USER, role="user", content="가" * 301)


# ---------------------------------------------------------------------------


EVIDENCE = "한국은행은 2026년 8월 27일 기준금리를 2.50%로 동결했다."
GOOD_ANSWER = "기준금리는 2.50%로 동결되었습니다 [{key}]."
BAD_ANSWER = "기준금리는 3.00%로 인상되었습니다 [{key}]."


def gen(text: str, model: str = "qwen3.5:27b", done_reason: str = "stop") -> dict:
    """/generate response as apps/ai returns it."""
    return {"model": model, "text": text, "done_reason": done_reason, "truncated": done_reason == "length"}


def fb(text: str, model: str = "gemini", finish_reason: str = "STOP") -> dict:
    """/fallback response as apps/ai returns it (Gemini finishReason)."""
    return {"model": model, "text": text, "finish_reason": finish_reason, "truncated": finish_reason == "MAX_TOKENS"}


@skipUnless(POSTGRES, "needs PostgreSQL + pgvector")
@override_settings(FASTAPI_INFRA_RETRY_DELAYS=(0, 0))
@no_redis
class WorkerTests(TestCase):
    def setUp(self):
        self.chunk = add_chunk(EVIDENCE, vec(0))
        self.key = self.chunk.citation_key
        self.job_id, _ = repository.create_question_job(USER, "8월 기준금리는?")

    def fake(self, **routes) -> FakeFastAPI:
        routes.setdefault("embed", {"model": "qwen3-embedding:0.6b", "embeddings": [vec(0)]})
        return FakeFastAPI(**routes)

    def job(self) -> Job:
        return Job.objects.get(id=self.job_id)

    def test_generate_judge_pass_saves_answer_in_one_go(self):
        fake = self.fake(
            generate=gen(GOOD_ANSWER.format(key=self.key)),
            judge={"model": "gemini", "unsupported_claims": []},
        )
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.SUCCEEDED)
        self.assertEqual(fake.calls, ["/embed", "/generate", "/judge"])

        job = self.job()
        answer = Answer.objects.get()
        self.assertEqual((job.answer_id, job.reused_answer), (answer.id, False))
        self.assertFalse(answer.is_fallback)
        self.assertEqual(answer.model_name, "qwen3.5:27b")
        self.assertEqual(list(answer.question_embedding), vec(0))
        self.assertEqual(list(AnswerCitation.objects.values_list("chunk_id", flat=True)), [self.chunk.id])
        self.assertEqual(job.assistant_message.answer_id, answer.id)
        self.assertTrue(answer.verification["generate"]["passed"])

        view = self.client.get(f"/api/qa/jobs/{self.job_id}", headers={"X-User-Id": str(USER)}).json()
        self.assertEqual(view["status"], "succeeded")
        self.assertEqual(view["answer"]["citations"][0]["key"], self.key)

    def test_similar_question_reuses_answer_without_generation(self):
        self.test_generate_judge_pass_saves_answer_in_one_go()
        job_id, _ = repository.create_question_job(USER, "8월 기준금리 알려줘")
        fake = self.fake()  # no generate/judge/fallback routes: calling them would KeyError
        self.assertEqual(process_job(job_id, fake.client()), Job.Status.SUCCEEDED)
        self.assertEqual(fake.calls, ["/embed"])
        job = Job.objects.get(id=job_id)
        self.assertTrue(job.reused_answer)
        self.assertEqual(Answer.objects.count(), 1)  # no new answer, no new vector

    def test_answer_citing_retired_chunk_is_not_reused(self):
        self.test_generate_judge_pass_saves_answer_in_one_go()
        DocumentChunk.objects.filter(id=self.chunk.id).update(retired_at=timezone.now())
        self.assertIsNone(repository.find_similar_answer(vec(0), 0.1))

    def test_code_check_failure_skips_judge_and_uses_fallback(self):
        fake = self.fake(
            generate=gen(BAD_ANSWER.format(key=self.key)),
            fallback=fb(GOOD_ANSWER.format(key=self.key)),
        )
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.SUCCEEDED)
        self.assertEqual(fake.calls, ["/embed", "/generate", "/fallback"])
        answer = Answer.objects.get()
        self.assertTrue(answer.is_fallback)
        self.assertEqual(answer.model_name, "gemini")
        self.assertEqual(answer.verification["generate"]["reasons"], ["unsupported_fact"])

    def test_truncated_generation_goes_to_fallback(self):
        fake = self.fake(
            generate=gen(GOOD_ANSWER.format(key=self.key), done_reason="length"),
            fallback=fb(GOOD_ANSWER.format(key=self.key)),
        )
        process_job(self.job_id, fake.client())
        self.assertIn("truncated", Answer.objects.get().verification["generate"]["reasons"])

    def test_judge_claims_go_to_fallback(self):
        fake = self.fake(
            generate=gen(GOOD_ANSWER.format(key=self.key)),
            judge={"model": "gemini", "unsupported_claims": ["동결 이유"]},
            fallback=fb(GOOD_ANSWER.format(key=self.key)),
        )
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.SUCCEEDED)
        self.assertEqual(fake.calls, ["/embed", "/generate", "/judge", "/fallback"])

    def test_truncated_fallback_fails_verification(self):
        fake = self.fake(
            generate=gen(BAD_ANSWER.format(key=self.key)),
            fallback=fb(GOOD_ANSWER.format(key=self.key), finish_reason="MAX_TOKENS"),
        )
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.FAILED)
        self.assertEqual(self.job().failure_type, Job.FailureType.VERIFICATION)

    def test_refusal_without_citation_passes(self):
        fake = self.fake(
            generate=gen("제공된 자료에서 확인할 수 없습니다."),
            judge={"model": "gemini", "unsupported_claims": []},
        )
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.SUCCEEDED)
        self.assertEqual(AnswerCitation.objects.count(), 0)

    def test_unconfigured_gemini_judge_goes_to_fallback_without_retry(self):
        not_configured = httpx.Response(
            503, json={"error": {"kind": "not_configured", "reason": "gemini_judge_model_not_set", "retryable": False}}
        )
        fake = self.fake(
            generate=gen(GOOD_ANSWER.format(key=self.key)),
            judge=not_configured,
            fallback=fb(GOOD_ANSWER.format(key=self.key)),
        )
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.SUCCEEDED)
        self.assertEqual(fake.calls, ["/embed", "/generate", "/judge", "/fallback"])
        self.assertEqual(self.job().infra_retries, 0)

    def test_no_evidence_fails_as_infra_without_generation(self):
        DocumentChunk.objects.update(retired_at=timezone.now())
        fake = self.fake()  # no generate route: calling it would KeyError
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.FAILED)
        self.assertEqual(fake.calls, ["/embed"])
        job = self.job()
        self.assertEqual((job.failure_type, job.failure_detail), (Job.FailureType.INFRA, "no_evidence"))
        self.assertEqual(repository.count_user_failures(USER), 0)

    def test_judge_error_goes_to_fallback(self):
        fake = self.fake(
            generate=gen(GOOD_ANSWER.format(key=self.key)),
            judge=429,
            fallback=fb(GOOD_ANSWER.format(key=self.key)),
        )
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.SUCCEEDED)
        self.assertEqual(fake.calls.count("/judge"), 3)  # 1 + 2 retries
        self.assertTrue(Answer.objects.get().is_fallback)

    def test_fallback_failure_fails_job_without_answer(self):
        fake = self.fake(
            generate=gen(BAD_ANSWER.format(key=self.key)),
            fallback=fb(BAD_ANSWER.format(key=self.key)),
        )
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.FAILED)
        job = self.job()
        self.assertEqual(job.failure_type, Job.FailureType.VERIFICATION)
        self.assertEqual(Answer.objects.count(), 0)
        self.assertEqual(Message.objects.filter(role="assistant").count(), 0)
        self.assertEqual(repository.count_user_failures(USER), 1)

    def test_infra_failure_is_retried_and_not_counted(self):
        fake = self.fake(generate=503)
        self.assertEqual(process_job(self.job_id, fake.client()), Job.Status.FAILED)
        job = self.job()
        self.assertEqual(job.failure_type, Job.FailureType.INFRA)
        self.assertEqual(job.infra_retries, 2)
        self.assertEqual(fake.calls.count("/generate"), 3)
        self.assertEqual(repository.count_user_failures(USER), 0)

    def test_job_is_run_once(self):
        fake = self.fake(generate=gen(GOOD_ANSWER.format(key=self.key)), judge={"model": "g", "unsupported_claims": []})
        process_job(self.job_id, fake.client())
        self.assertIsNone(process_job(self.job_id, fake.client()))  # popped twice: skipped

    def test_reaped_job_discards_late_result(self):
        def generate(payload):
            Job.objects.filter(id=self.job_id).update(
                status=Job.Status.FAILED, failure_type=Job.FailureType.INFRA
            )
            return gen(GOOD_ANSWER.format(key=self.key))

        fake = self.fake(generate=generate, judge={"model": "g", "unsupported_claims": []})
        self.assertIsNone(process_job(self.job_id, fake.client()))
        self.assertEqual(Answer.objects.count(), 0)  # rolled back with the job update


@skipUnless(POSTGRES, "needs PostgreSQL + pgvector")
class RetrievalTests(TestCase):
    def test_top_chunks_fit_char_budget_in_distance_order(self):
        near = add_chunk("가" * 1500, vec(0))
        mid = add_chunk("나" * 600, vec(0, 1))
        far = add_chunk("다" * 400, vec(0, 1, 2))
        add_chunk("라" * 10, vec(5))  # orthogonal: ranked last
        chunks = repository.search_chunks(vec(0), limit=3, max_chars=2000)
        self.assertEqual([c.chunk_id for c in chunks], [near.id, far.id])  # mid (600) does not fit after 1500
        self.assertEqual([c.document_id for c in chunks], [near.citation_key, far.citation_key])
        self.assertLessEqual(sum(len(c.text) for c in chunks), 2000)

    def test_top_chunk_longer_than_budget_is_cut(self):
        add_chunk("가" * 2500, vec(0))
        chunks = repository.search_chunks(vec(0), limit=5, max_chars=2000)
        self.assertEqual([len(c.text) for c in chunks], [2000])

    def test_retired_chunks_are_not_searched(self):
        chunk = add_chunk("x", vec(0))
        DocumentChunk.objects.filter(id=chunk.id).update(retired_at=timezone.now())
        self.assertEqual(repository.search_chunks(vec(0), limit=5, max_chars=2000), ())

    def test_reindex_retires_old_chunks(self):
        repository.save_document(
            external_id="a.md", title="a", source_uri="a.md", content_sha256="1" * 64,
            chunks=[("one", vec(0)), ("two", vec(1))], embedding_model="emb",
        )
        repository.save_document(
            external_id="a.md", title="a", source_uri="a.md", content_sha256="2" * 64,
            chunks=[("three", vec(2))], embedding_model="emb",
        )
        self.assertEqual(SourceDocument.objects.count(), 1)
        self.assertEqual(repository.get_document_hash("a.md"), "2" * 64)
        active = DocumentChunk.objects.filter(retired_at__isnull=True)
        self.assertEqual(list(active.values_list("content", flat=True)), ["three"])
        self.assertEqual(DocumentChunk.objects.count(), 3)


@skipUnless(POSTGRES, "needs PostgreSQL + pgvector")
class ReaperTests(TestCase):
    def test_stale_jobs_fail_as_infra(self):
        stale_id, _ = repository.create_question_job(USER, "old")
        fresh_id, _ = repository.create_question_job(USER, "new")
        Job.objects.filter(id=stale_id).update(
            status=Job.Status.RUNNING, started_at=timezone.now() - timedelta(hours=2)
        )
        self.assertEqual(repository.reap_stale_jobs(running_seconds=3600, queued_seconds=7200), [stale_id])
        self.assertEqual(Job.objects.get(id=stale_id).failure_type, Job.FailureType.INFRA)
        self.assertEqual(Job.objects.get(id=fresh_id).status, Job.Status.QUEUED)
        self.assertIsInstance(fresh_id, uuid.UUID)
