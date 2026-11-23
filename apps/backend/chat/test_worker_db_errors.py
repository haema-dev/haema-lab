"""워커의 DB 연결 장애 처리. DB 없이(SimpleTestCase 는 쿼리를 막는다) 전부 mock 으로 돌린다."""

from unittest.mock import MagicMock, call, patch

from django.db import InterfaceError, OperationalError
from django.test import SimpleTestCase, override_settings

from chat.management.commands import run_worker
from chat.management.commands.run_worker import Command, release_for_retry
from chat.models import Message

WORKER = "chat.management.commands.run_worker"
DB_DOWN = OperationalError("server closed the connection unexpectedly")


@override_settings(WORKER_DB_RETRY_DELAYS=(2, 4))
class WorkerDbErrorTests(SimpleTestCase):
    def setUp(self):
        for target in ("refresh_db_connections", "drop_db_connections"):
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
            self.command()._handle_message(7, client=MagicMock(), once=False)
        release.assert_called_once_with(7)
        message_model.objects.filter.assert_not_called()  # FAILED/internal_error 로 닫지 않는다
        self.drop_db_connections.assert_called()
        self.refresh_db_connections.assert_called_once()

    def test_interface_error_is_treated_as_db_error_too(self):
        with patch(f"{WORKER}.process_message", side_effect=InterfaceError("connection already closed")), \
                patch(f"{WORKER}.release_for_retry") as release:
            self.command()._handle_message(8, client=MagicMock(), once=False)
        release.assert_called_once_with(8)

    def test_release_is_retried_with_backoff_until_db_returns(self):
        with patch(f"{WORKER}.process_message", side_effect=DB_DOWN), \
                patch(f"{WORKER}.release_for_retry", side_effect=[DB_DOWN, DB_DOWN, DB_DOWN, None]) as release:
            self.command()._handle_message(9, client=MagicMock(), once=False)
        self.assertEqual(release.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(4)])  # 마지막 지연을 계속 쓴다

    def test_once_mode_does_not_loop_forever(self):
        with patch(f"{WORKER}.process_message", side_effect=DB_DOWN), \
                patch(f"{WORKER}.release_for_retry", side_effect=DB_DOWN):
            with self.assertRaises(OperationalError):
                self.command()._handle_message(10, client=MagicMock(), once=True)
        self.sleep.assert_not_called()

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
            self.command()._handle_message(11, client=MagicMock(), once=False)  # 예외가 밖으로 나오지 않는다
        self.drop_db_connections.assert_called_once()

    def test_crash_with_db_up_still_marks_failed(self):
        with patch(f"{WORKER}.process_message", side_effect=RuntimeError("bug")), \
                patch(f"{WORKER}.Message") as message_model:
            message_model.Status = Message.Status
            self.command()._handle_message(12, client=MagicMock(), once=False)
        update = message_model.objects.filter.return_value.update
        self.assertEqual(update.call_args.kwargs["status"], Message.Status.FAILED)
        self.assertEqual(update.call_args.kwargs["error"], "internal_error")

    def test_startup_requeue_waits_for_db_instead_of_crashing(self):
        cmd = Command()

        def stop_after_first_poll(timeout_seconds):
            cmd._stop = True
            return None

        with patch(f"{WORKER}.requeue_stale", side_effect=[DB_DOWN, 0]) as requeue, \
                patch(f"{WORKER}.FastAPIClient"), \
                patch(f"{WORKER}.dequeue_message", side_effect=stop_after_first_poll):
            cmd.handle(once=False)
        self.assertEqual(requeue.call_count, 2)
        self.sleep.assert_called_once_with(2)


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
