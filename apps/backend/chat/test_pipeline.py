from unittest.mock import patch

from django.test import override_settings

from chat._testing import ChatTestCase, FakeClient, no_gpu_slot, unit_vec
from chat.fastapi_client import FastAPIError, InfraError
from chat.models import AnswerCache, Message
from chat.pipeline import process_message
from chat.redis_queue import GpuBusyError
from chat.verify import REFUSAL_TEXT


@override_settings(FASTAPI_INFRA_RETRY_DELAYS=())  # 재시도 대기(5, 10, 20초)를 없애 테스트를 빠르게
class PipelineTests(ChatTestCase):
    def setUp(self):
        patcher = patch("chat.pipeline.gpu_slot", no_gpu_slot)
        patcher.start()
        self.addCleanup(patcher.stop)
        enqueue = patch("chat.pipeline.enqueue_message")
        self.enqueued = enqueue.start()
        self.addCleanup(enqueue.stop)
        self.chunk = self.make_chunk("2026-08-27 기준금리를 2.50%로 동결했다.", unit_vec(0))
        self.far = self.make_chunk("2026-07-10 다른 사건이다.", unit_vec(1), index=1)
        self.good = f"2026년 8월 27일 기준금리는 2.50%로 동결됐다. [{self.chunk.id}]"

    def run_message(self, client, **kwargs):
        message = self.make_message(**kwargs)
        status = process_message(message.pk, client=client)
        message.refresh_from_db()
        return status, message

    def test_generated_answer_is_saved_and_cached(self):
        client = FakeClient(answer=self.good)
        status, message = self.run_message(client)
        self.assertEqual(status, "done")
        self.assertEqual(message.status, Message.Status.DONE)
        self.assertEqual(message.source, Message.Source.GENERATED)
        self.assertEqual(message.answer, self.good)
        self.assertEqual(message.cited_chunk_ids, [self.chunk.id])
        self.assertTrue(message.verification["passed"])
        cache = AnswerCache.objects.get()
        self.assertEqual((cache.question, cache.answer, cache.hit_count), (message.question, self.good, 0))
        self.assertEqual(cache.cited_chunk_ids, [self.chunk.id])
        self.assertIsNotNone(cache.question_embedding)

    def test_question_is_embedded_with_instruction_and_chunks_are_not_instructed(self):
        client = FakeClient(answer=self.good)
        self.run_message(client)
        self.assertEqual(len(client.embed_texts), 1)
        self.assertTrue(client.embed_texts[0].startswith("Instruct: "))
        self.assertIn("Query: 2026년 8월 기준금리는?", client.embed_texts[0])

    def test_closest_chunk_comes_first_in_the_prompt(self):
        client = FakeClient(answer=self.good)
        self.run_message(client)
        prompt = client.prompts[0]
        self.assertLess(prompt.index(f"[{self.chunk.id}]"), prompt.index(f"[{self.far.id}]"))
        self.assertIn("질문: 2026년 8월 기준금리는?", prompt)

    @override_settings(RAG_TOP_K=1)
    def test_only_top_k_chunks_reach_the_prompt(self):
        client = FakeClient(answer=self.good)
        self.run_message(client)
        self.assertIn(f"[{self.chunk.id}]", client.prompts[0])
        self.assertNotIn(f"[{self.far.id}]", client.prompts[0])

    def test_exact_cache_hit_skips_the_model(self):
        AnswerCache.objects.create(
            question="2026년 8월 기준금리는?", answer="캐시 답 [1]", cited_chunk_ids=[1], verification={"passed": True}
        )
        client = FakeClient(answer="should not be used")
        status, message = self.run_message(client)
        self.assertEqual((status, message.source, message.answer), ("done", Message.Source.CACHE, "캐시 답 [1]"))
        self.assertEqual(message.cited_chunk_ids, [1])
        self.assertEqual((client.embed_texts, client.prompts), ([], []))
        self.assertEqual(AnswerCache.objects.get().hit_count, 1)

    @override_settings(CACHE_MIN_SIMILARITY=0.9)
    def test_similar_cache_skips_generation_when_enabled(self):
        AnswerCache.objects.create(
            question="다른 표현", question_embedding=unit_vec(0), answer="캐시 답", verification={}
        )
        client = FakeClient(answer="should not be used")
        status, message = self.run_message(client)
        self.assertEqual((message.source, message.answer), (Message.Source.CACHE, "캐시 답"))
        self.assertEqual(client.prompts, [])

    def test_similar_cache_is_off_by_default(self):
        AnswerCache.objects.create(question="다른 표현", question_embedding=unit_vec(0), answer="old", verification={})
        client = FakeClient(answer=self.good)
        _, message = self.run_message(client)
        self.assertEqual(message.source, Message.Source.GENERATED)

    def test_failed_code_check_is_rejected_and_not_stored_or_cached(self):
        client = FakeClient(answer=f"기준금리는 3.00%로 동결됐다. [{self.chunk.id}]")
        status, message = self.run_message(client)
        self.assertEqual((status, message.status), ("rejected", Message.Status.REJECTED))
        self.assertEqual(message.error, "verification_failed")
        self.assertIsNone(message.answer)
        self.assertIn("unsupported_fact", message.verification["reasons"])
        self.assertEqual(AnswerCache.objects.count(), 0)

    def test_no_chunks_answers_with_refusal_without_generation(self):
        self.chunk.delete()
        self.far.delete()
        client = FakeClient(answer="should not be used")
        status, message = self.run_message(client)
        self.assertEqual((status, message.answer), ("done", REFUSAL_TEXT))
        self.assertTrue(message.verification["no_evidence"])
        self.assertEqual(client.prompts, [])
        self.assertEqual(AnswerCache.objects.count(), 0)

    def test_infrastructure_failure_goes_back_to_pending_and_is_requeued(self):
        for client in (FakeClient(embed_error=InfraError("503")), FakeClient(generate_error=InfraError("503"))):
            self.enqueued.reset_mock()
            status, message = self.run_message(client)
            self.assertEqual((status, message.status, message.retry_count), ("pending", Message.Status.PENDING, 1))
            self.assertIn("infra", message.error)
            self.enqueued.assert_called_once_with(message.pk)

    def test_gpu_busy_is_also_a_retry_not_a_user_failure(self):
        def busy():
            raise GpuBusyError("timeout")

        with patch("chat.pipeline.gpu_slot", busy):
            status, message = self.run_message(FakeClient(answer=self.good))
        self.assertEqual((status, message.retry_count), ("pending", 1))

    @override_settings(JOB_MAX_ATTEMPTS=2)
    def test_retries_stop_after_max_attempts(self):
        client = FakeClient(embed_error=InfraError("503"))
        message = self.make_message()
        self.assertEqual(process_message(message.pk, client=client), "pending")
        self.assertEqual(process_message(message.pk, client=client), "failed")
        message.refresh_from_db()
        self.assertEqual((message.status, message.retry_count), (Message.Status.FAILED, 2))
        self.assertEqual(self.enqueued.call_count, 1)  # 두 번째 실패는 다시 큐에 넣지 않는다

    def test_gateway_rejection_fails_without_retry(self):
        status, message = self.run_message(FakeClient(generate_error=FastAPIError("422")))
        self.assertEqual((status, message.retry_count), ("failed", 0))
        self.assertTrue(message.error.startswith("gateway_rejected"))
        self.enqueued.assert_not_called()

    def test_empty_question_is_rejected(self):
        status, message = self.run_message(FakeClient(), question="   ")
        self.assertEqual((status, message.error), ("rejected", "empty_question"))

    def test_message_is_taken_only_once(self):
        message = self.make_message()
        client = FakeClient(answer=self.good)
        self.assertEqual(process_message(message.pk, client=client), "done")
        self.assertEqual(process_message(message.pk, client=client), "skipped")  # 이미 끝남
        self.assertEqual(process_message(987654321, client=client), "skipped")  # 삭제됨
        self.assertEqual(len(client.prompts), 1)
