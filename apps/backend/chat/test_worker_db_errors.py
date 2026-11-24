"""워커의 DB 연결 장애 처리와 Streams ack. DB·Redis 없이(SimpleTestCase 는 쿼리를 막는다) 전부 mock 으로 돌린다."""

from unittest.mock import MagicMock, call, patch

from django.db import InterfaceError, OperationalError
from django.test import SimpleTestCase, override_settings

from chat.management.commands import run_worker
from chat.management.commands.run_worker import Command, release_for_retry
from chat.models import Message
from chat.redis_queue import Job

WORKER = "chat.management.commands.run_worker"
DB_DOWN = OperationalError("server closed the connection unexpectedly")


@override_settings(WORKER_DB_RETRY_DELAYS=(2, 4))
class WorkerDbErrorTests(SimpleTestCase):
    def setUp(self):
        for target in ("refresh_db_connections", "drop_db_connections", "ack"):
            patcher = patch(f"{WORKER}.{target}")
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        sleep = patch(f"{WORKER}.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def command(self):
        cmd = Command()
        cmd._stop = False
        cmd._db_delays = (2, 4)
        return cmd

    def test_db_error_during_processing_releases_message_instead_of_failing_it(self):
        with patch(f"{WORKER}.process_message", side_effect=DB_DOWN), \
                patch(f"{WORKER}.release_for_retry") as release, \
                patch(f"{WORKER}.Message") as message_model:
            self.command()._handle_job(Job("7-0", 7), client=MagicMock(), once=False)
        release.assert_called_once_with(7)
        message_model.objects.filter.assert_not_called()  # FAILED/internal_error 로 닫지 않는다
        self.drop_db_connections.assert_called()
        self.refresh_db_connections.assert_called_once()
        self.ack.assert_called_once_with(Job("7-0", 7))  # 새 항목으로 다시 넣었으니 지금 항목은 ack

    def test_interface_error_is_treated_as_db_error_too(self):
        with patch(f"{WORKER}.process_message", side_effect=InterfaceError("connection already closed")), \
                patch(f"{WORKER}.release_for_retry") as release:
            self.command()._handle_job(Job("8-0", 8), client=MagicMock(), once=False)
        release.assert_called_once_with(8)

    def test_release_is_retried_with_backoff_until_db_returns(self):
        with patch(f"{WORKER}.process_message", side_effect=DB_DOWN), \
                patch(f"{WORKER}.release_for_retry", side_effect=[DB_DOWN, DB_DOWN, DB_DOWN, None]) as release:
            self.command()._handle_job(Job("9-0", 9), client=MagicMock(), once=False)
        self.assertEqual(release.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(4)])  # 마지막 지연을 계속 쓴다
        self.ack.assert_called_once()

    def test_once_mode_does_not_loop_forever(self):
        with patch(f"{WORKER}.process_message", side_effect=DB_DOWN), \
                patch(f"{WORKER}.release_for_retry", side_effect=DB_DOWN):
            with self.assertRaises(OperationalError):
                self.command()._handle_job(Job("10-0", 10), client=MagicMock(), once=True)
        self.sleep.assert_not_called()
        self.ack.assert_not_called()  # 되돌리지 못했으면 ack 하지 않는다: pending 목록에 남는다

    def test_stop_signal_ends_retry_loop(self):
        cmd = self.command()

        def fail_and_stop():
            cmd._stop = True
            raise DB_DOWN

        with self.assertRaises(OperationalError):
            cmd._with_db_retry(fail_and_stop, once=False, what="x")
        self.sleep.assert_not_called()

    def test_crash_while_db_is_down_does_not_kill_worker(self):
        with patch(f"{WORKER}.process_message", side_effect=RuntimeError("bug")), \
                patch(f"{WORKER}.Message") as message_model:
            message_model.Status = Message.Status
            message_model.objects.filter.return_value.update.side_effect = DB_DOWN
            self.command()._handle_job(Job("11-0", 11), client=MagicMock(), once=False)  # 예외가 밖으로 나오지 않는다
        self.drop_db_connections.assert_called_once()
        self.ack.assert_not_called()  # failed 로 닫지 못했으니 나중에 다시 가져와 닫는다

    def test_crash_with_db_up_still_marks_failed(self):
        with patch(f"{WORKER}.process_message", side_effect=RuntimeError("bug")), \
                patch(f"{WORKER}.Message") as message_model:
            message_model.Status = Message.Status
            self.command()._handle_job(Job("12-0", 12), client=MagicMock(), once=False)
        update = message_model.objects.filter.return_value.update
        self.assertEqual(update.call_args.kwargs["status"], Message.Status.FAILED)
        self.assertEqual(update.call_args.kwargs["error"], "internal_error")
        self.ack.assert_called_once_with(Job("12-0", 12))

    def test_startup_requeue_waits_for_db_instead_of_crashing(self):
        cmd = Command()

        def stop_after_first_poll(consumer, timeout_seconds):
            cmd._stop = True
            return None

        with (
            patch(f"{WORKER}.requeue_stale", side_effect=[DB_DOWN, 0]) as requeue,
            patch(f"{WORKER}.FastAPIClient"),
            patch(f"{WORKER}.ensure_group"),
            patch(f"{WORKER}.pending_message_ids", return_value=set()),
            patch(f"{WORKER}.cleanup_consumers", return_value=0),
            patch(f"{WORKER}.claim_stale", return_value=[]),
            patch(f"{WORKER}.dequeue_job", side_effect=stop_after_first_poll),
        ):
            cmd.handle(once=False)
        self.assertEqual(requeue.call_count, 2)
        self.sleep.assert_called_once_with(2)


class StreamWorkerTests(SimpleTestCase):
    """Streams: 처리 결과가 DB 에 남은 뒤에만 ack, 죽은 워커의 항목은 processing -> pending 으로 되돌리고 이어서 처리."""

    def setUp(self):
        self.patches = {}
        for target in ("refresh_db_connections", "ensure_group", "requeue_stale", "ack", "FastAPIClient",
                       "pending_message_ids", "cleanup_consumers", "heartbeat"):
            patcher = patch(f"{WORKER}.{target}")
            self.patches[target] = patcher.start()
            self.addCleanup(patcher.stop)
        self.patches["requeue_stale"].return_value = 0
        self.patches["pending_message_ids"].return_value = {99}
        self.patches["cleanup_consumers"].return_value = 0

    def run_once(self, *, new=(), reclaimed=(), process=None):
        feed = list(new) + [None]
        with (
            patch(f"{WORKER}.claim_stale", return_value=list(reclaimed)) as claim,
            patch(f"{WORKER}.dequeue_job", side_effect=feed),
            patch(f"{WORKER}.reset_reclaimed") as reset,
            patch(f"{WORKER}.process_message", side_effect=process or (lambda mid, client: "done")) as proc,
        ):
            Command().handle(once=True)
        return claim, reset, proc

    def test_new_jobs_are_processed_then_acked(self):
        jobs = [Job("1-0", 1), Job("2-0", 2)]
        _, reset, proc = self.run_once(new=jobs)
        self.assertEqual([c.args[0] for c in proc.call_args_list], [1, 2])
        self.assertEqual([c.args[0] for c in self.patches["ack"].call_args_list], jobs)
        reset.assert_not_called()
        self.patches["ensure_group"].assert_called_once()

    def test_reclaimed_jobs_are_reset_to_pending_before_processing(self):
        job = Job("5-0", 42)
        claim, reset, proc = self.run_once(reclaimed=[job])
        claim.assert_called_once()
        reset.assert_called_once_with(42)
        proc.assert_called_once()
        self.patches["ack"].assert_called_once_with(job)

    def test_ack_failure_does_not_crash_worker(self):
        import redis

        self.patches["ack"].side_effect = redis.ConnectionError("down")
        _, _, proc = self.run_once(new=[Job("1-0", 1), Job("2-0", 2)])
        self.assertEqual(proc.call_count, 2)  # 다음 job 도 처리됨


class ReleaseForRetryTests(SimpleTestCase):
    def test_reverts_only_processing_and_requeues_without_counting_a_retry(self):
        with patch(f"{WORKER}.Message") as message_model, patch(f"{WORKER}.enqueue_message") as enqueue:
            message_model.Status = Message.Status
            release_for_retry(5)
        message_model.objects.filter.assert_called_once_with(pk=5, status=Message.Status.PROCESSING)
        update_kwargs = message_model.objects.filter.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs["status"], Message.Status.PENDING)
        self.assertNotIn("retry_count", update_kwargs)
        enqueue.assert_called_once_with(5)


class DropConnectionsTests(SimpleTestCase):
    def test_close_errors_are_swallowed(self):
        broken = MagicMock()
        broken.close.side_effect = DB_DOWN
        with patch.object(run_worker, "connections") as conns:
            conns.all.return_value = [broken]
            run_worker.drop_db_connections()
        conns.all.assert_called_once_with(initialized_only=True)


class RefreshConnectionsTests(SimpleTestCase):
    """job 사이 연결 정리: 트랜잭션(TestCase 의 atomic) 안의 연결은 닫지 않는다 (CI test_worker 깨짐 재발 방지)."""

    def test_skips_connections_inside_atomic_block(self):
        in_tx, idle = MagicMock(in_atomic_block=True), MagicMock(in_atomic_block=False)
        with patch.object(run_worker, "connections") as conns:
            conns.all.return_value = [in_tx, idle]
            run_worker.refresh_db_connections()
        in_tx.close_if_unusable_or_obsolete.assert_not_called()
        idle.close_if_unusable_or_obsolete.assert_called_once()


class HeartbeatAndRecoveryTests(SimpleTestCase):
    def test_heartbeat_keeps_a_long_job_alive(self):
        import time as real_time

        def slow(message_id, client):
            real_time.sleep(0.08)
            return "done"

        with (
            patch(f"{WORKER}.HEARTBEAT_SECONDS", 0.01),
            patch(f"{WORKER}.heartbeat") as beat,
            patch(f"{WORKER}.process_message", side_effect=slow),
            patch(f"{WORKER}.ack"),
            patch(f"{WORKER}.refresh_db_connections"),
        ):
            cmd = Command()
            cmd._stop, cmd._db_delays, cmd._consumer = False, (2,), "pod-a"
            cmd._handle_job(Job("3-0", 3), client=MagicMock(), once=False)
        self.assertGreaterEqual(beat.call_count, 2)
        beat.assert_called_with("pod-a", Job("3-0", 3))

    def test_heartbeat_stops_after_the_job(self):
        with (
            patch(f"{WORKER}.HEARTBEAT_SECONDS", 0.01),
            patch(f"{WORKER}.heartbeat") as beat,
            patch(f"{WORKER}.process_message", return_value="done"),
            patch(f"{WORKER}.ack"),
            patch(f"{WORKER}.refresh_db_connections"),
        ):
            cmd = Command()
            cmd._stop, cmd._db_delays, cmd._consumer = False, (2,), "pod-a"
            cmd._handle_job(Job("4-0", 4), client=MagicMock(), once=False)
            calls = beat.call_count
            import time as real_time
            real_time.sleep(0.05)
        self.assertEqual(beat.call_count, calls)

    def test_requeue_stale_skips_messages_still_in_the_stream(self):
        with patch(f"{WORKER}.Message") as message_model, patch(f"{WORKER}.enqueue_message") as enqueue:
            message_model.Status = Message.Status
            message_model.objects.filter.return_value.values_list.return_value = [1, 2, 3]
            self.assertEqual(run_worker.requeue_stale(600, exclude={2}), 2)
        self.assertEqual([c.args[0] for c in enqueue.call_args_list], [1, 3])

    @override_settings(WORKER_CLAIM_IDLE_SECONDS=90)
    def test_claim_uses_short_idle_and_cleans_old_consumers(self):
        cmd = Command()

        def stop(consumer, timeout_seconds):
            cmd._stop = True

        with (
            patch(f"{WORKER}.FastAPIClient"),
            patch(f"{WORKER}.ensure_group"),
            patch(f"{WORKER}.pending_message_ids", return_value=set()),
            patch(f"{WORKER}.requeue_stale", return_value=0),
            patch(f"{WORKER}.consumer_name", return_value="pod-a"),
            patch(f"{WORKER}.cleanup_consumers", return_value=2) as cleanup,
            patch(f"{WORKER}.claim_stale", return_value=[]) as claim,
            patch(f"{WORKER}.dequeue_job", side_effect=stop),
        ):
            cmd.handle(once=False)
        claim.assert_called_once_with("pod-a", 90)
        cleanup.assert_called_once_with("pod-a", run_worker.CONSUMER_IDLE_CLEANUP_SECONDS)
