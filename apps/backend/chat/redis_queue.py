"""Redis: 모델이 필요한 요청의 job 큐(Streams)와 GPU 세마포어.

* 큐: API 가 message_id 를 XADD 로 한 번 넣고, 워커가 컨슈머 그룹으로 XREADGROUP 해서 끝까지 처리한 뒤 XACK + XDEL.
  처리 중 워커가 죽으면 그 항목은 그룹의 pending 목록(PEL)에 남고, 다른 워커가 XAUTOCLAIM 으로 이어받는다.
  일반 DB 조회·캐시 히트는 큐를 거치지 않는다.
* 길이 제한(MAXLEN)은 쓰지 않는다: 처리되지 않은 요청을 조용히 버린다. 처리가 끝난 항목만 XDEL 로 지운다.
  쌓이는 양은 API 의 유저당 동시 처리 상한(MAX_PENDING_PER_USER)이 막는다.
* GPU 세마포어: iGPU 를 쓰는 단계(/embed, /generate)만 슬롯을 잡는다. Gemini 호출은 잡지 않는다.
"""

import logging
import os
import socket
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import redis
from django.conf import settings

log = logging.getLogger(__name__)

STREAM_KEY = "chat:jobs:stream"
GROUP = "workers"
FIELD = "message_id"
# List(LPUSH/BRPOP) 시절의 큐. 키 타입이 달라 같은 이름을 쓸 수 없다. 남은 항목은 워커 시작 때 Streams 로 옮긴다.
LEGACY_LIST_KEY = "chat:jobs"
GPU_SLOT_KEY_PREFIX = "chat:gpu-slot:"

# 내가 잡은 슬롯일 때만 지운다(TTL이 끝나 남이 잡은 슬롯을 지우지 않도록).
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


@dataclass(frozen=True)
class Job:
    """큐에서 꺼낸 항목 하나. entry_id 로 ack 한다."""

    entry_id: str
    message_id: int


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


def consumer_name() -> str:
    """컨슈머 이름: 파드 이름(HOSTNAME). 파드가 바뀌면 새 이름이 되고, 옛 이름의 pending 은 XAUTOCLAIM 이 가져간다."""
    return os.environ.get("HOSTNAME") or socket.gethostname()


def ensure_group() -> None:
    """스트림과 컨슈머 그룹을 만든다(이미 있으면 그대로). 워커 시작 때 한 번 부른다."""
    try:
        get_redis().xgroup_create(STREAM_KEY, GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def migrate_legacy_list() -> int:
    """List 큐에 남은 message_id 를 Streams 로 옮긴다. 옮긴 개수를 돌려준다. 없으면 0."""
    client = get_redis()
    moved = 0
    while (item := client.rpop(LEGACY_LIST_KEY)) is not None:
        client.xadd(STREAM_KEY, {FIELD: str(item)})
        moved += 1
    return moved


def enqueue_message(message_id: int) -> None:
    get_redis().xadd(STREAM_KEY, {FIELD: str(message_id)})


def _to_jobs(entries) -> list[Job]:
    """XREADGROUP/XAUTOCLAIM 의 (entry_id, fields) 목록을 Job 으로. message_id 가 없거나 숫자가 아니면 ack 하고 버린다."""
    jobs = []
    for entry_id, fields in entries or []:
        raw = (fields or {}).get(FIELD, "")
        if isinstance(raw, str) and raw.isascii() and raw.isdigit():
            jobs.append(Job(entry_id=entry_id, message_id=int(raw)))
        else:
            log.warning("dropping malformed queue entry %s: %r", entry_id, fields)
            ack(Job(entry_id=entry_id, message_id=0))
    return jobs


def dequeue_job(consumer: str, timeout_seconds: int = 5) -> Job | None:
    """새 항목 하나를 이 컨슈머 몫으로 가져온다. 없으면 timeout_seconds 동안 기다렸다 None."""
    result = get_redis().xreadgroup(GROUP, consumer, {STREAM_KEY: ">"}, count=1, block=timeout_seconds * 1000)
    if not result:
        return None
    _stream, entries = result[0]
    jobs = _to_jobs(entries)
    return jobs[0] if jobs else None


def claim_stale(consumer: str, min_idle_seconds: int, count: int = 10) -> list[Job]:
    """min_idle_seconds 넘게 하트비트가 없는(=처리하던 워커가 죽은) 항목을 이 컨슈머로 가져온다."""
    result = get_redis().xautoclaim(
        STREAM_KEY, GROUP, consumer, min_idle_time=min_idle_seconds * 1000, start_id="0-0", count=count
    )
    # redis-py: [next_start_id, [(id, fields), ...], [deleted_ids]] (Redis 7)
    return _to_jobs(result[1] if result and len(result) > 1 else [])


def heartbeat(consumer: str, job: Job) -> None:
    """처리 중인 항목의 idle 시간을 0 으로 되돌린다(소유자는 그대로). 살아 있는 job 이 claim_stale 에 회수되지 않게 한다."""
    get_redis().xclaim(STREAM_KEY, GROUP, consumer, min_idle_time=0, message_ids=[job.entry_id], justid=True)


def pending_message_ids(limit: int = 1000) -> set[int]:
    """지금 그룹의 pending 목록(누군가 처리 중이거나 회수를 기다림)에 있는 message_id. 시작 시 DB 복구에서 뺀다."""
    client = get_redis()
    entries = client.xpending_range(STREAM_KEY, GROUP, min="-", max="+", count=limit)
    ids: set[int] = set()
    for entry in entries:
        for _entry_id, fields in client.xrange(STREAM_KEY, min=entry["message_id"], max=entry["message_id"]):
            raw = (fields or {}).get(FIELD, "")
            if raw.isascii() and raw.isdigit():
                ids.add(int(raw))
    return ids


def cleanup_consumers(exclude: str, min_idle_seconds: int) -> int:
    """처리 중인 항목이 없고 오래 조용한 컨슈머(재배포로 사라진 파드)를 그룹에서 지운다. 지운 수를 돌려준다.

    pending 이 0 인 컨슈머만 지우므로 잃는 항목이 없다. 혹시 살아 있던 컨슈머라도 다음 XREADGROUP 때 다시 생긴다.
    """
    client = get_redis()
    removed = 0
    for info in client.xinfo_consumers(STREAM_KEY, GROUP):
        if info["name"] != exclude and info["pending"] == 0 and info["idle"] >= min_idle_seconds * 1000:
            client.xgroup_delconsumer(STREAM_KEY, GROUP, info["name"])
            removed += 1
    return removed


def ack(job: Job) -> None:
    """처리가 끝난 항목: pending 목록에서 빼고(XACK) 스트림에서도 지운다(XDEL)."""
    pipe = get_redis().pipeline(transaction=True)
    pipe.xack(STREAM_KEY, GROUP, job.entry_id)
    pipe.xdel(STREAM_KEY, job.entry_id)
    pipe.execute()


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
