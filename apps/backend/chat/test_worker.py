import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from chat._testing import ChatTestCase, FakeClient, no_gpu_slot, unit_vec
from chat.management.commands.run_worker import requeue_stale
from chat.models import Message, SourceChunk

WORKER = "chat.management.commands.run_worker"


@override_settings(FASTAPI_INFRA_RETRY_DELAYS=())
class WorkerTests(ChatTestCase):
    def setUp(self):
        for target, new in (("chat.pipeline.gpu_slot", no_gpu_slot),):
            patcher = patch(target, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_worker(self, ids, client):
        feed = list(ids) + [None]
        with patch(f"{WORKER}.dequeue_message", side_effect=feed), patch(f"{WORKER}.FastAPIClient", return_value=client):
            call_command("run_worker", "--once")

    def test_worker_processes_queued_messages_to_done(self):
        chunk = self.make_chunk("2026-08-27 기준금리를 2.50%로 동결했다.", unit_vec(0))
        answer = f"2026년 8월 27일 기준금리는 2.50%로 동결됐다. [{chunk.id}]"
        first, second = self.make_message("질문 하나"), self.make_message("질문 둘")
        self.run_worker([first.pk, second.pk], FakeClient(answer=answer))
        for message in (first, second):
            message.refresh_from_db()
            self.assertEqual((message.status, message.answer), (Message.Status.DONE, answer))

    def test_crash_in_one_message_does_not_stop_the_worker(self):
        ok = self.make_message("정상")
        bad = self.make_message("고장")
        original = __import__("chat.pipeline", fromlist=["process_message"]).process_message

        def flaky(message_id, *, client):
            if message_id == bad.pk:
                raise RuntimeError("bug")
            return original(message_id, client=client)

        with patch(f"{WORKER}.process_message", flaky):
            self.run_worker([bad.pk, ok.pk], FakeClient(answer="x"))
        bad.refresh_from_db()
        ok.refresh_from_db()
        self.assertEqual((bad.status, bad.error), (Message.Status.FAILED, "internal_error"))
        self.assertNotEqual(ok.status, Message.Status.PENDING)  # 그다음 메시지도 처리됨

    def test_stale_processing_messages_are_requeued(self):
        stale = self.make_message("오래됨", status=Message.Status.PROCESSING)
        fresh = self.make_message("방금", status=Message.Status.PROCESSING)
        Message.objects.filter(pk=stale.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        with patch(f"{WORKER}.enqueue_message") as enqueue:
            self.assertEqual(requeue_stale(600), 1)
        enqueue.assert_called_once_with(stale.pk)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual((stale.status, fresh.status), (Message.Status.PENDING, Message.Status.PROCESSING))


class LoadChunksTests(ChatTestCase):
    def write(self, rows):
        path = Path(tempfile.mkdtemp()) / "chunks.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        return path

    def load(self, path):
        with patch("chat.management.commands.load_chunks.FastAPIClient", return_value=FakeClient()):
            call_command("load_chunks", str(path))

    def test_indexes_and_is_idempotent_and_updates_changed_content(self):
        row = {"url": "https://example.test/a", "title": "A", "published_at": "2026-08-27", "chunk_index": 0, "content": "본문"}
        path = self.write([row, {**row, "chunk_index": 1, "content": "둘째"}])
        self.load(path)
        self.assertEqual(SourceChunk.objects.count(), 2)
        self.assertEqual(SourceChunk.objects.get(chunk_index=0).document.title, "A")
        self.assertEqual(len(SourceChunk.objects.get(chunk_index=0).embedding), 1024)
        self.load(path)  # 같은 내용: 그대로
        self.assertEqual(SourceChunk.objects.count(), 2)
        self.load(self.write([{**row, "content": "바뀐 본문"}]))
        self.assertEqual(SourceChunk.objects.get(chunk_index=0).content, "바뀐 본문")
