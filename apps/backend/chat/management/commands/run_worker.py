"""Redis 큐(chat:jobs)에서 message_id 를 하나씩 꺼내 끝까지 처리하는 워커.

실행: python manage.py run_worker        (API 와 다른 프로세스, 같은 코드)
      python manage.py run_worker --once (큐가 빌 때까지 처리하고 종료: 테스트용)

LPUSH/BRPOP 큐에는 ack 가 없어서 워커가 처리 도중 죽으면 그 메시지는 processing 으로 남는다.
그래서 시작할 때 오래된 processing 을 pending 으로 되돌려 다시 큐에 넣는다.
"""

import logging
import signal
import time
from datetime import timedelta

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from chat.fastapi_client import FastAPIClient
from chat.models import Message
from chat.pipeline import process_message
from chat.redis_queue import dequeue_message, enqueue_message

log = logging.getLogger(__name__)
DEFAULT_STALE_SECONDS = 600  # 생성 한 건이 길어야 약 2분: 10분 넘게 processing 이면 죽은 것으로 본다(추정)


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
        client = FastAPIClient()
        try:
            stale = requeue_stale(getattr(settings, "PROCESSING_STALE_SECONDS", DEFAULT_STALE_SECONDS))
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
                try:
                    status = process_message(message_id, client=client)
                    log.info("message %s -> %s", message_id, status)
                except Exception:
                    log.exception("message %s crashed", message_id)
                    # 큐에서 이미 꺼냈으므로 pending 으로 두면 영영 처리되지 않는다: 닫는다.
                    Message.objects.filter(
                        pk=message_id, status__in=[Message.Status.PENDING, Message.Status.PROCESSING]
                    ).update(status=Message.Status.FAILED, error="internal_error", updated_at=timezone.now())
            log.info("worker stopped")
        finally:
            client.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
