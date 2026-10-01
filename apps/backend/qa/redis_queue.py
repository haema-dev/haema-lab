"""Redis: the job queue, the "job finished" notice and the GPU semaphore.

* Queue: the API pushes a job_id once (LPUSH); one worker pops it once (BRPOP).
  Only requests that need a model go through the queue.
* Notice: after the final DB commit the worker PUBLISHes the job_id. This is a
  status signal for SSE listeners, not a queue; polling clients ignore it.
* GPU semaphore: only steps that run on the Node B iGPU (/embed, /generate)
  hold a slot. Gemini calls (/judge, /fallback) do not.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import redis
from django.conf import settings

QUEUE_KEY = "qa:jobs"
DONE_CHANNEL_PREFIX = "qa:job-done:"
GPU_SLOT_KEY_PREFIX = "qa:gpu-slot:"

# Delete the slot only if we still own it; a slot whose TTL expired may belong to someone else.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class GpuBusyError(Exception):
    """No GPU slot freed up within the wait limit."""


_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        # The socket timeout must outlast the BRPOP wait, or an empty queue reads as an error.
        _client = redis.Redis.from_url(
            settings.REDIS_URL, decode_responses=True, socket_timeout=30, socket_connect_timeout=5
        )
    return _client


def enqueue_job(job_id: uuid.UUID | str) -> None:
    get_redis().lpush(QUEUE_KEY, str(job_id))


def dequeue_job(timeout_seconds: int = 5) -> str | None:
    item = get_redis().brpop([QUEUE_KEY], timeout=timeout_seconds)
    return item[1] if item else None


def done_channel(job_id: uuid.UUID | str) -> str:
    return f"{DONE_CHANNEL_PREFIX}{job_id}"


def notify_job_done(job_id: uuid.UUID | str, status: str) -> None:
    get_redis().publish(done_channel(job_id), status)


@contextmanager
def gpu_slot(
    *,
    slots: int | None = None,
    ttl_seconds: int | None = None,
    wait_seconds: float | None = None,
    poll_seconds: float = 0.5,
) -> Iterator[None]:
    """Hold one of ``slots`` GPU slots across all workers.

    The TTL frees a slot whose worker died mid-call; it must exceed the
    longest GPU call (the FastAPI timeout).
    """
    client = get_redis()
    slots = slots or settings.QA_GPU_SLOTS
    ttl_ms = int((ttl_seconds or settings.QA_GPU_SLOT_TTL_SECONDS) * 1000)
    deadline = time.monotonic() + (wait_seconds or settings.QA_GPU_WAIT_SECONDS)
    token = uuid.uuid4().hex

    held_key: str | None = None
    while held_key is None:
        for index in range(slots):
            key = f"{GPU_SLOT_KEY_PREFIX}{index}"
            if client.set(key, token, nx=True, px=ttl_ms):
                held_key = key
                break
        else:
            if time.monotonic() >= deadline:
                raise GpuBusyError("GPU slot wait timed out")
            time.sleep(poll_seconds)
    try:
        yield
    finally:
        client.eval(_RELEASE_SCRIPT, 1, held_key, token)
