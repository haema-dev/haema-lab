"""Django worker: pops job_ids from Redis and runs each to the end.

    uv run python manage.py run_qa_worker

Same image as the API; run it as a separate Deployment.
"""

from __future__ import annotations

import logging
import signal
import time

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from qa import repository
from qa.fastapi_client import FastAPIClient
from qa.models import Job
from qa.redis_queue import dequeue_job, notify_job_done
from qa.worker import process_job

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the question worker (Redis queue -> FastAPI -> DB)."

    def handle(self, *args, **options) -> None:
        stopping = False

        def stop(signum, frame) -> None:
            nonlocal stopping
            stopping = True  # finish the current job, then exit

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        client = FastAPIClient()
        next_reap = 0.0
        try:
            while not stopping:
                if time.monotonic() >= next_reap:
                    close_old_connections()
                    reaped = repository.reap_stale_jobs(
                        running_seconds=settings.QA_JOB_RUNNING_TIMEOUT_SECONDS,
                        queued_seconds=settings.QA_JOB_QUEUED_TIMEOUT_SECONDS,
                    )
                    if reaped:
                        logger.warning("reaped %d stale jobs", len(reaped))
                    # Reaping committed a final state; SSE listeners wait for the same notice.
                    for job_id in reaped:
                        try:
                            notify_job_done(job_id, Job.Status.FAILED)
                        except redis.RedisError:
                            logger.warning("job %s done notice failed", job_id)
                    next_reap = time.monotonic() + 60

                try:
                    job_id = dequeue_job(timeout_seconds=5)
                except redis.RedisError as exc:
                    logger.warning("queue read failed, retrying: %s", exc)
                    time.sleep(2)
                    continue
                if job_id is None:
                    continue
                close_old_connections()
                status = process_job(job_id, client)
                logger.info("job %s -> %s", job_id, status)
        finally:
            client.close()
