"""Redis Streams 큐(chat:jobs:stream, 그룹 workers)에서 message_id 를 하나씩 꺼내 끝까지 처리하는 워커.

실행: python manage.py run_worker        (API 와 다른 프로세스, 같은 코드)
      python manage.py run_worker --once (큐가 빌 때까지 처리하고 종료: 테스트용)

처리가 끝나야(done/rejected/failed/재시도로 다시 넣음) ack 한다. 처리하는 동안 HEARTBEAT_SECONDS 마다 하트비트로
항목의 idle 시간을 0 으로 되돌린다. 워커가 죽으면 하트비트가 끊기고, CLAIM_IDLE_SECONDS 뒤 살아 있는(또는 재시작된)
워커가 XAUTOCLAIM 으로 가져와 이어서 처리한다. job 이 아무리 길어도 살아 있는 한 회수되지 않는다(중복 처리 없음).
시작할 때 List 시절 큐에 남은 항목을 Streams 로 옮기고, 큐 밖에서 processing 으로 남은 메시지(List 시절 등)도 되살린다.

DB 연결 장애(OperationalError/InterfaceError: 서버 재시작, 연결 끊김)는 인프라 실패다.
유저 실패로 닫지 않고(retry_count 도 올리지 않는다) 연결을 버린 뒤 기다렸다가 메시지를 다시 큐에 넣는다.
"""

import logging
import signal
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import InterfaceError, OperationalError, connections
from django.utils import timezone

from chat import notify
from chat.fastapi_client import FastAPIClient
from chat.models import Message
from chat.pipeline import process_message
from chat.redis_queue import (
    Job,
    ack,
    claim_stale,
    cleanup_consumers,
    consumer_name,
    dequeue_job,
    enqueue_message,
    ensure_group,
    heartbeat,
    migrate_legacy_list,
    pending_message_ids,
)

log = logging.getLogger(__name__)
DEFAULT_STALE_SECONDS = 600  # 생성 한 건이 길어야 약 2분: 10분 넘게 processing 이면 죽은 것으로 본다(추정)
CLAIM_INTERVAL_SECONDS = 30  # 죽은 워커의 항목을 찾는 주기
HEARTBEAT_SECONDS = 15  # 처리 중 하트비트 간격
# 하트비트가 이만큼 끊기면 처리하던 워커가 죽은 것으로 본다(하트비트 6번 놓침). job 길이와 무관하다.
DEFAULT_CLAIM_IDLE_SECONDS = 90
CONSUMER_IDLE_CLEANUP_SECONDS = 3600  # pending 0 이고 1시간 조용한 컨슈머(사라진 파드)는 그룹에서 지운다
REDIS_ERRORS = (redis.ConnectionError, redis.TimeoutError)
DB_ERRORS = (OperationalError, InterfaceError)
DEFAULT_DB_RETRY_DELAYS = (2, 4, 8, 16, 30)  # 초. 마지막 값을 계속 쓴다(추정: Postgres 재시작이 30초 안에 끝난다고 봄)


def requeue_stale(max_age_seconds: int, exclude: set[int] | frozenset[int] = frozenset()) -> int:
    """큐 밖에서 오래 processing 으로 남은 메시지를 pending 으로 되돌려 큐에 다시 넣는다.

    exclude: 스트림 pending 목록에 있는 message_id. 처리 중(하트비트 중)이거나 claim_stale 이 회수할 것이라 건드리지 않는다.
    """
    cutoff = timezone.now() - timedelta(seconds=max_age_seconds)
    ids = [
        pk
        for pk in Message.objects.filter(status=Message.Status.PROCESSING, updated_at__lt=cutoff).values_list(
            "pk", flat=True
        )
        if pk not in exclude
    ]
    for message_id in ids:
        Message.objects.filter(pk=message_id, status=Message.Status.PROCESSING).update(
            status=Message.Status.PENDING, updated_at=timezone.now()
        )
        enqueue_message(message_id)
    return len(ids)


def reset_reclaimed(message_id: int) -> None:
    """죽은 워커에게서 가져온 메시지: processing 이면 pending 으로 되돌려야 process_message 가 다시 잡는다."""
    Message.objects.filter(pk=message_id, status=Message.Status.PROCESSING).update(
        status=Message.Status.PENDING, updated_at=timezone.now()
    )


def release_for_retry(message_id: int) -> None:
    """DB 장애로 중단된 메시지를 다시 처리할 수 있게 돌려놓는다.

    이 워커가 processing 으로 잡은 뒤 끊겼다면 pending 으로 되돌린다(아니면 process_message 가 skipped 로 넘긴다).
    retry_count 는 올리지 않는다: DB 장애는 유저 실패도, 모델 인프라 재시도 한도 대상도 아니다.
    """
    Message.objects.filter(pk=message_id, status=Message.Status.PROCESSING).update(
        status=Message.Status.PENDING, updated_at=timezone.now()
    )
    enqueue_message(message_id)


def refresh_db_connections() -> None:
    """job 사이에 쓸 수 없게 된(끊긴·오래된) 연결을 정리한다. django.db.close_old_connections 와 같되,

    트랜잭션(atomic) 안의 연결은 건드리지 않는다. close_old_connections 는 autocommit 이 꺼진 연결을 무조건 닫아서
    TestCase(테스트마다 atomic) 안에서 워커를 돌리면 테스트 DB 연결이 닫힌다. 운영 워커는 job 사이에 트랜잭션 밖이다.
    """
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close_if_unusable_or_obsolete()


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
        stale_seconds = getattr(settings, "PROCESSING_STALE_SECONDS", DEFAULT_STALE_SECONDS)
        claim_idle = getattr(settings, "WORKER_CLAIM_IDLE_SECONDS", DEFAULT_CLAIM_IDLE_SECONDS)
        consumer = self._consumer = consumer_name()
        client = FastAPIClient()
        try:
            self._with_redis_retry(ensure_group, once=once)
            moved = self._with_redis_retry(migrate_legacy_list, once=once)
            if moved:
                log.warning("moved %s jobs from the legacy list queue to the stream", moved)
            in_stream = self._with_redis_retry(pending_message_ids, once=once)
            stale = self._with_db_retry(
                lambda: requeue_stale(stale_seconds, exclude=in_stream), once=once, what="requeue_stale"
            )
            if stale:
                log.warning("requeued %s stale processing messages", stale)
            log.info("worker started consumer=%s", consumer)
            next_claim = 0.0
            while not self._stop:
                try:
                    reclaimed = []
                    if time.monotonic() >= next_claim:
                        # 배포 중에는 옛 API 파드가 아직 List 에 넣을 수 있다: 주기마다 옮긴다(비어 있으면 RPOP 한 번).
                        if moved := migrate_legacy_list():
                            log.warning("moved %s jobs from the legacy list queue to the stream", moved)
                        reclaimed = claim_stale(consumer, claim_idle)
                        if removed := cleanup_consumers(consumer, CONSUMER_IDLE_CLEANUP_SECONDS):
                            log.info("removed %s idle consumers", removed)
                        next_claim = time.monotonic() + CLAIM_INTERVAL_SECONDS
                    if reclaimed:
                        log.warning("reclaimed %s jobs from dead consumers", len(reclaimed))
                        jobs = [(job, True) for job in reclaimed]
                    else:
                        job = dequeue_job(consumer, timeout_seconds=1 if once else 5)
                        jobs = [(job, False)] if job else []
                except REDIS_ERRORS:
                    if once:
                        raise
                    log.warning("redis unavailable, retrying in 2s", exc_info=True)
                    notify.error("worker.redis_unavailable", "워커: Redis 연결 실패", "2초 뒤 다시 시도합니다.")
                    time.sleep(2)
                    continue
                if not jobs:
                    if once:
                        break
                    continue
                for job, was_reclaimed in jobs:
                    if self._stop:
                        break  # 남은 항목은 ack 하지 않았으므로 pending 목록에 남아 다른 워커가 가져간다
                    self._handle_job(job, client, once=once, reclaimed=was_reclaimed)
            log.info("worker stopped")
        finally:
            client.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _handle_job(self, job: Job, client: FastAPIClient, *, once: bool, reclaimed: bool = False) -> None:
        """job 하나를 끝까지 처리하고 ack 한다. 결과를 DB 에 남기지 못했으면 ack 하지 않는다(나중에 다시 가져간다)."""
        message_id = job.message_id
        # 오래 도는 프로세스라 요청 경계가 없다: 쓸 수 없게 된 연결은 job 마다 정리한다.
        refresh_db_connections()
        log.info("job req message=%s entry=%s%s", message_id, job.entry_id, " reclaimed" if reclaimed else "")
        t0 = time.monotonic()
        if reclaimed:
            self._with_db_retry(lambda: reset_reclaimed(message_id), once=once, what=f"reset {message_id}")
        settled = True
        try:
            with self._heartbeat(job):
                status = process_message(message_id, client=client)
            log.log(
                logging.INFO if status in ("done", "skipped", "pending") else logging.WARNING,
                "job res message=%s status=%s dur=%.1fs", message_id, status, time.monotonic() - t0,
            )
        except DB_ERRORS as exc:
            log.warning("job res message=%s status=db_error dur=%.1fs error=%s: %s (재시도)", message_id,
                        time.monotonic() - t0, type(exc).__name__, exc)
            drop_db_connections()
            # pending 으로 되돌리고 새 항목으로 다시 넣는다. 그다음 지금 항목을 ack 한다.
            self._with_db_retry(lambda: release_for_retry(message_id), once=once, what=f"release {message_id}")
        except Exception as exc:
            log.exception("message %s crashed", message_id)
            notify.error("worker.crash", "워커: job 처리 중 예외", f"message {message_id}: {type(exc).__name__}: {exc}"[:500])
            # 같은 항목을 다시 처리해도 또 죽을 가능성이 크다: failed 로 닫고 ack 한다.
            try:
                Message.objects.filter(
                    pk=message_id, status__in=[Message.Status.PENDING, Message.Status.PROCESSING]
                ).update(status=Message.Status.FAILED, error="internal_error", updated_at=timezone.now())
            except DB_ERRORS as exc:
                # 닫지 못했으면 ack 하지 않는다: pending 목록에 남아 DB 가 돌아온 뒤 다시 가져와 닫는다.
                log.warning("could not mark message %s failed: %s: %s", message_id, type(exc).__name__, exc)
                drop_db_connections()
                settled = False
        if settled:
            self._ack(job)

    @contextmanager
    def _heartbeat(self, job: Job):
        """처리하는 동안 별도 스레드에서 HEARTBEAT_SECONDS 마다 항목의 idle 시간을 되돌린다."""
        consumer = getattr(self, "_consumer", None) or consumer_name()
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    heartbeat(consumer, job)
                except REDIS_ERRORS as exc:
                    log.warning("heartbeat failed for entry %s: %s", job.entry_id, exc)

        thread = threading.Thread(target=beat, name=f"heartbeat-{job.entry_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)

    def _ack(self, job: Job) -> None:
        try:
            ack(job)
        except REDIS_ERRORS as exc:
            # ack 를 못 해도 결과는 DB 에 있다. 나중에 다시 가져가면 process_message 가 skipped 로 넘기고 그때 ack 된다.
            log.warning("ack failed for entry %s (message %s): %s", job.entry_id, job.message_id, exc)

    def _with_redis_retry(self, call, *, once: bool):
        """Redis 가 돌아올 때까지 call 을 다시 시도한다(시작 단계용). --once 이거나 종료 신호를 받으면 그만둔다."""
        while True:
            try:
                return call()
            except REDIS_ERRORS:
                if once or self._stop:
                    raise
                log.warning("redis unavailable during startup, retrying in 2s", exc_info=True)
                time.sleep(2)

    def _with_db_retry(self, call, *, once: bool, what: str):
        """DB 가 돌아올 때까지 call 을 다시 시도한다. --once(테스트)이거나 종료 신호를 받으면 그만둔다."""
        attempt = 0
        while True:
            try:
                return call()
            except DB_ERRORS as exc:
                if once or self._stop:
                    raise
                delay = self._db_delays[min(attempt, len(self._db_delays) - 1)] if self._db_delays else 0
                attempt += 1
                log.warning("database unavailable during %s, retry %s in %ss: %s: %s", what, attempt, delay,
                            type(exc).__name__, exc)
                notify.error("worker.db_unavailable", "워커: DB 연결 실패",
                             f"{what} 중 {type(exc).__name__}: {exc}"[:500])
                drop_db_connections()
                time.sleep(delay)
