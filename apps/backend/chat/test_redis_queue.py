"""Redis Streams 큐 함수. Redis 없이 get_redis() 를 MagicMock 으로 바꿔 보낸 명령만 확인한다."""

from unittest.mock import MagicMock, patch

import redis
from django.test import SimpleTestCase

from chat import redis_queue as q
from chat.redis_queue import Job


class StreamQueueTests(SimpleTestCase):
    def setUp(self):
        self.r = MagicMock()
        patcher = patch.object(q, "get_redis", return_value=self.r)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_enqueue_uses_xadd_without_maxlen(self):
        q.enqueue_message(7)
        self.r.xadd.assert_called_once_with(q.STREAM_KEY, {"message_id": "7"})  # MAXLEN 없음: 미처리 요청을 버리지 않는다

    def test_dequeue_reads_new_entries_for_this_consumer(self):
        self.r.xreadgroup.return_value = [[q.STREAM_KEY, [("1700-0", {"message_id": "42"})]]]
        self.assertEqual(q.dequeue_job("pod-a", timeout_seconds=5), Job("1700-0", 42))
        self.r.xreadgroup.assert_called_once_with(q.GROUP, "pod-a", {q.STREAM_KEY: ">"}, count=1, block=5000)

    def test_dequeue_timeout_returns_none(self):
        self.r.xreadgroup.return_value = []
        self.assertIsNone(q.dequeue_job("pod-a"))

    def test_ack_removes_from_pending_and_stream(self):
        pipe = self.r.pipeline.return_value
        q.ack(Job("1700-0", 42))
        pipe.xack.assert_called_once_with(q.STREAM_KEY, q.GROUP, "1700-0")
        pipe.xdel.assert_called_once_with(q.STREAM_KEY, "1700-0")
        pipe.execute.assert_called_once()

    def test_claim_stale_takes_idle_entries(self):
        self.r.xautoclaim.return_value = ["0-0", [("1-0", {"message_id": "5"}), ("2-0", {"message_id": "6"})], []]
        jobs = q.claim_stale("pod-b", min_idle_seconds=600)
        self.assertEqual(jobs, [Job("1-0", 5), Job("2-0", 6)])
        kwargs = self.r.xautoclaim.call_args.kwargs
        self.assertEqual((kwargs["min_idle_time"], kwargs["start_id"]), (600_000, "0-0"))

    def test_malformed_entry_is_acked_and_dropped(self):
        self.r.xreadgroup.return_value = [[q.STREAM_KEY, [("9-0", {"other": "x"})]]]
        self.assertIsNone(q.dequeue_job("pod-a"))
        self.r.pipeline.return_value.xack.assert_called_once_with(q.STREAM_KEY, q.GROUP, "9-0")

    def test_ensure_group_tolerates_existing_group(self):
        self.r.xgroup_create.side_effect = redis.ResponseError("BUSYGROUP Consumer Group name already exists")
        q.ensure_group()  # 예외 없음
        self.r.xgroup_create.assert_called_once_with(q.STREAM_KEY, q.GROUP, id="0", mkstream=True)

    def test_ensure_group_raises_other_errors(self):
        self.r.xgroup_create.side_effect = redis.ResponseError("WRONGTYPE")
        with self.assertRaises(redis.ResponseError):
            q.ensure_group()

    def test_legacy_list_items_move_to_stream_oldest_first(self):
        self.r.rpop.side_effect = ["1", "2", None]  # LPUSH 로 쌓였으니 RPOP 이 오래된 것부터
        self.assertEqual(q.migrate_legacy_list(), 2)
        self.assertEqual([c.args[1] for c in self.r.xadd.call_args_list], [{"message_id": "1"}, {"message_id": "2"}])
        self.r.rpop.assert_called_with(q.LEGACY_LIST_KEY)

    def test_heartbeat_resets_idle_without_changing_owner(self):
        q.heartbeat("pod-a", Job("1700-0", 42))
        self.r.xclaim.assert_called_once_with(
            q.STREAM_KEY, q.GROUP, "pod-a", min_idle_time=0, message_ids=["1700-0"], justid=True
        )

    def test_pending_message_ids_reads_ids_of_pending_entries(self):
        self.r.xpending_range.return_value = [{"message_id": "1-0"}, {"message_id": "2-0"}]
        self.r.xrange.side_effect = [[("1-0", {"message_id": "5"})], [("2-0", {"message_id": "6"})]]
        self.assertEqual(q.pending_message_ids(), {5, 6})

    def test_cleanup_removes_only_idle_consumers_with_nothing_pending(self):
        self.r.xinfo_consumers.return_value = [
            {"name": "pod-a", "pending": 0, "idle": 10**9},        # 자기 자신: 남긴다
            {"name": "old-1", "pending": 0, "idle": 4_000_000},    # 지운다
            {"name": "old-2", "pending": 1, "idle": 10**9},        # 처리 중인 항목이 있음: 남긴다(회수 대상)
            {"name": "new-1", "pending": 0, "idle": 1_000},        # 최근 활동: 남긴다
        ]
        self.assertEqual(q.cleanup_consumers("pod-a", min_idle_seconds=3600), 1)
        self.r.xgroup_delconsumer.assert_called_once_with(q.STREAM_KEY, q.GROUP, "old-1")
