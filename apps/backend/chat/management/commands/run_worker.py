"""Redis 큐(chat:jobs)에서 message_id 를 하나씩 꺼내 끝까지 처리하는 워커.

실행: python manage.py run_worker        (API 와 다른 프로세스, 같은 코드)
      python manage.py run_worker --once (큐가 빌 때까지 처리하고 종료: 테스트용)

LPUSH/BRPOP 큐에는 ack 가 없어서 워커가 처리 도중 죽으면 그 메시지는 processing 으로 남는다.
그래서 시작할 때 오래된 processing 을 pending 으로 되돌려 다시 큐에 넣는다.

DB 연결 장애(OperationalError/InterfaceError: 서버 재시작, 연결 끊김)는 인프라 실패다.
유저 실패로 닫지 않고(retry_count 도 올리지 않는다) 연결을 버린 뒤 기다렸다가 메시지를 다시 큐에 넣는다.
"""

import logging
import signal
import time
from datetime import timedelta

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import InterfaceError, OperationalError, close_old_connections, connections
from django.utils import timezone

from chat.fastapi_client import FastAPIClient
from chat.models import Message
from chat.pipeline import process_message
from chat.redis_queue import dequeue_message, enqueue_message

log = logging.getLogger(__name__)
DEFAULT_STALE_SECONDS = 600  # 생성 한 건이 길어야 약 2분: 10분 넘게 processing 이면 죽은 것으로 본다(추정)
DB_ERRORS = (OperationalError, InterfaceError)
DEFAULT_DB_RETRY_DELAYS = (2, 4, 8, 16, 30)  # 초. 마지막 값을 계속 쓴다(추정: Postgres 재시작이 30초 안에 끝난다고 봄)


def requeue_stale(max_age_seconds: int) -> int:
    """오래 processing 인 메시지를 pending 으로 되돌려 큐에 다시 넣는다."""
    cutoff = timezone.now() - timedelta(seconds=max_age_seconds)
    ids = list(
        Message.objects.filter(status=Message.Status.PROCESSING, updated_at__lt=cutoff).values_list("pk", flat=True)
    )
    for message_id in ids:
        Message.objects.filter(pk=message_id, status=Message.Status.PROCESSING).update(
            status=Message.Status.PENDING, updated_at=timezone.now()
        )
        enqueue_message(message_id)
    return len(ids)


def release_for_retry(message_id: int) -> None:
    """DB 장애로 중단된 메시지를 다시 처리할 수 있게 돌려놓는다.

    이 워커가 processing 으로 잡은 뒤 끊겼다면 pending 으로 되돌린다(아니면 process_message 가 skipped 로 넘긴다).
    retry_count 는 올리지 않는다: DB 장애는 유저 실패도, 모델 인프라 재시도 한도 대상도 아니다.
    """
    Message.objects.filter(pk=message_id, status=Message.Status.PROCESSING).update(
        status=Message.Status.PENDING, updated_at=timezone.now()
    )
    enqueue_message(message_id)


def drop_db_connections() -> None:
    """끊긴 연결을 붙잡고 있으면 다음 쿼리도 같은 에러가 난다: 버리고 다음 쿼리에서 새로 연결한다."""
    for conn in connections.all(initialized_only=True):
        try:
            conn.close()
        except DB_ERRORS:
            pass


class Command(BaseCommand):
    help = "Process chat jobs from the Redis queue."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="큐가 비면 종료한다(테스트용)")

    def handle(self, *args, once: bool, **options):
        self._stop = False
        previous = {
            sig: signal.signal(sig, lambda *_: setattr(self, "_stop", True))
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
        self._db_delays = tuple(getattr(settings, "WORKER_DB_RETRY_DELAYS", DEFAULT_DB_RETRY_DELAYS))
        client = FastAPIClient()
        try:
            stale_seconds = getattr(settings, "PROCESSING_STALE_SECONDS", DEFAULT_STALE_SECONDS)
            stale = self._with_db_retry(lambda: requeue_stale(stale_seconds), once=once, what="requeue_stale")
            if stale:
                log.warning("requeued %s stale processing messages", stale)
            log.info("worker started")
            while not self._stop:
                try:
                    message_id = dequeue_message(timeout_seconds=1 if once else 5)
                except (redis.ConnectionError, redis.TimeoutError):
                    if once:
                        raise
                    log.warning("redis unavailable, retrying in 2s", exc_info=True)
                    time.sleep(2)
                    continue
                if message_id is None:
                    if once:
                        break
                    continue
                self._handle_message(message_id, client, once=once)
            log.info("worker stopped")
        finally:
            client.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _handle_message(self, message_id: int, client: FastAPIClient, *, once: bool) -> None:
        # 오래 도는 프로세스라 요청 경계가 없다: 쓸 수 없게 된 연결은 job 마다 정리한다.
        close_old_connections()
        log.info("job req message=%s", message_id)
        t0 = time.monotonic()
        try:
            status = process_message(message_id, client=client)
            log.log(
                logging.INFO if status in ("done", "skipped", "pending") else logging.WARNING,
                "job res message=%s status=%s dur=%.1fs", message_id, status, time.monotonic() - t0,
            )
        except DB_ERRORS:
            log.warning("message %s interrupted by database error, will retry", message_id, exc_info=True)
            drop_db_connections()
            self._with_db_retry(lambda: release_for_retry(message_id), once=once, what=f"release {message_id}")
        except Exception:
            log.exception("message %s crashed", message_id)
            # 큐에서 이미 꺼냈으므로 pending 으로 두면 영영 처리되지 않는다: 닫는다.
            try:
                Message.objects.filter(
                    pk=message_id, status__in=[Message.Status.PENDING, Message.Status.PROCESSING]
                ).update(status=Message.Status.FAILED, error="internal_error", updated_at=timezone.now())
            except DB_ERRORS:
                # 닫지 못하면 processing 으로 남고, 다음 시작 때 requeue_stale 이 다시 넣는다.
                log.warning("could not mark message %s failed: database unavailable", message_id, exc_info=True)
                drop_db_connections()

    def _with_db_retry(self, call, *, once: bool, what: str):
        """DB 가 돌아올 때까지 call 을 다시 시도한다. --once(테스트)이거나 종료 신호를 받으면 그만둔다."""
        attempt = 0
        while True:
            try:
                return call()
            except DB_ERRORS:
                if once or self._stop:
                    raise
                delay = self._db_delays[min(attempt, len(self._db_delays) - 1)] if self._db_delays else 0
                attempt += 1
                log.warning("database unavailable during %s, retry %s in %ss", what, attempt, delay, exc_info=True)
                drop_db_connections()
                time.sleep(delay)
