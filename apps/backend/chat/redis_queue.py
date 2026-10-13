"""Redis: 모델이 필요한 요청의 job 큐와 GPU 세마포어.

* 큐: API가 message_id를 한 번 LPUSH, 워커가 한 번 BRPOP. 일반 DB 조회·캐시 히트는 거치지 않는다.
* GPU 세마포어: iGPU를 쓰는 단계(/embed, /generate)만 슬롯을 잡는다. Gemini 호출은 잡지 않는다.
"""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import redis
from django.conf import settings

QUEUE_KEY = "chat:jobs"
GPU_SLOT_KEY_PREFIX = "chat:gpu-slot:"

# 내가 잡은 슬롯일 때만 지운다(TTL이 끝나 남이 잡은 슬롯을 지우지 않도록).
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class GpuBusyError(Exception):
    """대기 시간 안에 GPU 슬롯이 나지 않았다."""


_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        # socket_timeout은 BRPOP 대기보다 길어야 빈 큐가 에러로 읽히지 않는다.
        _client = redis.Redis.from_url(
            settings.REDIS_URL, decode_responses=True, socket_timeout=30, socket_connect_timeout=5
        )
    return _client


def enqueue_message(message_id: int) -> None:
    get_redis().lpush(QUEUE_KEY, str(message_id))


def dequeue_message(timeout_seconds: int = 5) -> int | None:
    item = get_redis().brpop([QUEUE_KEY], timeout=timeout_seconds)
    return int(item[1]) if item else None


@contextmanager
def gpu_slot(poll_seconds: float = 0.5) -> Iterator[None]:
    """전체 워커에 걸쳐 GPU 슬롯 하나를 잡는다. TTL은 가장 긴 GPU 호출(FastAPI 타임아웃)보다 길어야 한다."""
    client = get_redis()
    ttl_ms = int(settings.GPU_SLOT_TTL_SECONDS * 1000)
    deadline = time.monotonic() + settings.GPU_WAIT_SECONDS
    token = uuid.uuid4().hex

    held_key = None
    while held_key is None:
        for index in range(settings.GPU_SLOTS):
            key = f"{GPU_SLOT_KEY_PREFIX}{index}"
            if client.set(key, token, nx=True, px=ttl_ms):
                held_key = key
                break
        else:
            if time.monotonic() >= deadline:
                raise GpuBusyError("GPU 슬롯 대기 시간 초과")
            time.sleep(poll_seconds)
    try:
        yield
    finally:
        client.eval(_RELEASE_SCRIPT, 1, held_key, token)
