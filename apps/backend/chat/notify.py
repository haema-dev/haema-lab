"""Discord 웹훅 알림: job 결과(토큰·시간 등 수치)와 에러.

URL 은 환경변수 DISCORD_WEBHOOK_URL(GitHub Secret -> backend-secret). 없으면 아무것도 보내지 않는다.
* 보내기는 별도 스레드에서 한다: 알림이 느리거나 실패해도 job 처리·응답에 영향을 주지 않는다(로그만 남김).
* URL 은 로그에 남기지 않는다(비밀값).
* 사용자 질문·답변 본문은 보내지 않는다. message id 와 길이·수치만 보낸다.
* 같은 에러(key)는 DISCORD_ERROR_COOLDOWN_SECONDS 안에 한 번만 보낸다(장애 중 도배 방지).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import httpx
from django.conf import settings

log = logging.getLogger(__name__)

USERNAME = "haema-lab"
COLORS = {"done": 0x2ECC71, "rejected": 0xF1C40F, "failed": 0xE74C3C, "error": 0xE74C3C, "pending": 0x95A5A6}
DEFAULT_ERROR_COOLDOWN_SECONDS = 300
_last_error_at: dict[str, float] = {}
_lock = threading.Lock()


def _url() -> str:
    return getattr(settings, "DISCORD_WEBHOOK_URL", "") or ""


def _post(payload: dict, url: str) -> None:
    try:
        response = httpx.post(url, json=payload, timeout=5)
    except httpx.HTTPError as exc:
        log.warning("discord webhook failed: %s", type(exc).__name__)
        return
    if response.status_code == 429:
        log.warning("discord webhook rate limited (429), dropped")
    elif response.status_code >= 400:
        log.warning("discord webhook HTTP %s", response.status_code)


def _dispatch(payload: dict, url: str) -> None:
    threading.Thread(target=_post, args=(payload, url), name="discord-webhook", daemon=True).start()


def send(payload: dict) -> bool:
    """보낼 URL 이 있으면 보내고 True. 없으면 False(아무것도 안 함)."""
    url = _url()
    if not url:
        return False
    _dispatch({"username": USERNAME, **payload}, url)
    return True


def _fmt(value, unit: str = "", digits: int | None = None) -> str:
    if value is None or value == "":
        return "-"
    if digits is not None and isinstance(value, (int, float)):
        value = f"{value:.{digits}f}"
    return f"{value}{unit}"


def job_fields(message, trace) -> list[dict]:
    """job 결과 수치. trace 는 pipeline.JobTrace(없으면 None)."""
    stages = getattr(trace, "stages", {}) or {}
    embed = getattr(trace, "embed", {}) or {}
    gen = getattr(trace, "generate", {}) or {}
    e2e = (message.updated_at - message.created_at).total_seconds() if message.created_at and message.updated_at else None
    reason = (message.error or "").split(":", 1)[0] or "-"
    verification = message.verification or {}
    pairs = [
        ("상태", message.status),
        ("출처", message.source or "-"),
        ("전체 (접수→끝)", _fmt(e2e, "초", 1)),
        ("처리 (워커)", _fmt(getattr(trace, "elapsed", None), "초", 1)),
        ("재시도", _fmt(message.retry_count)),
        ("사유", reason),
        ("임베딩", _fmt(stages.get("embed"), "초", 2)),
        ("검색", _fmt(stages.get("search"), "초", 2)),
        ("생성", _fmt(stages.get("generate"), "초", 1)),
        ("프롬프트 토큰", _fmt(gen.get("prompt_tokens"))),
        ("출력 토큰", _fmt(gen.get("output_tokens"))),
        ("생성 속도", _fmt(gen.get("output_tok_s"), " tok/s", 2)),
        ("프롬프트 처리", _fmt(gen.get("prompt_tok_s"), " tok/s", 1)),
        ("TTFT", _fmt(gen.get("ttft_s"), "초", 2)),
        ("모델 로드", _fmt(gen.get("load_s", embed.get("load_s")), "초", 2)),
        ("답변 길이", _fmt(len(message.answer) if message.answer else None, "자")),
        ("인용", _fmt(len(message.cited_chunk_ids or []))),
        ("검증", "통과" if verification.get("passed") else ("-" if not verification else "실패")),
    ]
    return [{"name": name, "value": str(value), "inline": True} for name, value in pairs]


def job_result(message, trace=None) -> bool:
    """끝난 job 하나(done/rejected/failed)의 수치를 보낸다. DISCORD_NOTIFY_JOBS=False 면 보내지 않는다."""
    if not getattr(settings, "DISCORD_NOTIFY_JOBS", True):
        return False
    embed = {
        "title": f"job {message.pk} · {message.status}",
        "color": COLORS.get(message.status, COLORS["pending"]),
        "fields": job_fields(message, trace),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return send({"embeds": [embed]})


def error(key: str, title: str, detail: str = "") -> bool:
    """에러 알림. 같은 key 는 쿨다운 동안 한 번만 보낸다. 보냈으면 True."""
    if not _url():
        return False
    cooldown = getattr(settings, "DISCORD_ERROR_COOLDOWN_SECONDS", DEFAULT_ERROR_COOLDOWN_SECONDS)
    now = time.monotonic()
    with _lock:
        last = _last_error_at.get(key)
        if last is not None and now - last < cooldown:
            return False
        _last_error_at[key] = now
    embed = {
        "title": f"⚠️ {title}"[:256],
        "description": (detail or "-")[:1500],
        "color": COLORS["error"],
        "fields": [{"name": "key", "value": key[:256], "inline": True},
                   {"name": "같은 알림 간격", "value": f"{cooldown}초", "inline": True}],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return send({"embeds": [embed]})
