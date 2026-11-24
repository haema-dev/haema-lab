"""테스트·평가 결과를 eval_run / eval_result 에 넣기 전에 검증·변환한다. DB 를 쓰지 않는 순수 함수.

입력은 두 가지다.
* JSON (사이클·골든셋·벤치 등 직접 만든 결과):
    {"kind": "cycle", "environment": "server", "data_kind": "real", "target": "cluster",
     "git_sha": "67fe178", "started_at": "2026-11-24T10:00:00+09:00", "finished_at": "...",
     "summary": {"e2e_s": 21.5}, "note": "...",
     "results": [{"name": "generate", "status": "pass", "duration_ms": 21500, "metrics": {...}, "message": null}]}
* JUnit XML (pytest --junitxml, Gradle build/test-results): 환경·데이터 종류는 XML 에 없으므로 따로 받는다.

환경(local/ci/server)과 데이터 종류(mock/sample/real)는 기본값 없이 반드시 받는다.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

KINDS = ("unit", "golden", "cycle", "bench")
ENVIRONMENTS = ("local", "ci", "server")
DATA_KINDS = ("mock", "sample", "real")
STATUSES = ("pass", "fail", "skip", "error")
MAX_MESSAGE_CHARS = 2_000
MAX_NAME_CHARS = 300


class EvalInputError(ValueError):
    """입력이 스키마에 맞지 않는다. 아무것도 저장하지 않는다."""


@dataclass(frozen=True)
class ResultIn:
    name: str
    status: str
    duration_ms: int | None = None
    metrics: dict = field(default_factory=dict)
    message: str | None = None


@dataclass(frozen=True)
class RunIn:
    kind: str
    environment: str
    data_kind: str
    started_at: datetime
    finished_at: datetime | None
    target: str = ""
    git_sha: str | None = None
    summary: dict = field(default_factory=dict)
    note: str = ""
    results: tuple[ResultIn, ...] = ()

    @property
    def passed(self) -> bool:
        """skip 을 뺀 모든 케이스가 pass. 케이스가 하나도 없으면 실패로 본다(아무것도 검증 안 됨)."""
        counted = [r for r in self.results if r.status != "skip"]
        return bool(counted) and all(r.status == "pass" for r in counted)


def _choice(value: object, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise EvalInputError(f"{name} 는 {'/'.join(allowed)} 중 하나여야 합니다: {value!r}")
    return value  # type: ignore[return-value]


def _aware(value: object, name: str, *, required: bool) -> datetime | None:
    if value is None:
        if required:
            raise EvalInputError(f"{name} 가 없습니다")
        return None
    if not isinstance(value, str):
        raise EvalInputError(f"{name} 는 ISO 8601 문자열이어야 합니다")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvalInputError(f"{name} 형식 오류: {value!r}") from exc
    if parsed.tzinfo is None:
        raise EvalInputError(f"{name} 에 시간대가 없습니다(예: +09:00): {value!r}")
    return parsed


def _dict(value: object, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EvalInputError(f"{name} 는 객체여야 합니다")
    return value


def _results(items: object) -> tuple[ResultIn, ...]:
    if not isinstance(items, list):
        raise EvalInputError("results 는 배열이어야 합니다")
    seen: set[str] = set()
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise EvalInputError(f"results[{i}] 는 객체여야 합니다")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
            raise EvalInputError(f"results[{i}].name 은 1~{MAX_NAME_CHARS}자 문자열이어야 합니다")
        if name in seen:
            raise EvalInputError(f"results 에 같은 name 이 두 번 있습니다: {name!r}")
        seen.add(name)
        duration = item.get("duration_ms")
        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration < 0):
            raise EvalInputError(f"results[{i}].duration_ms 는 0 이상 정수여야 합니다")
        message = item.get("message")
        if message is not None and not isinstance(message, str):
            raise EvalInputError(f"results[{i}].message 는 문자열이어야 합니다")
        out.append(
            ResultIn(
                name=name,
                status=_choice(item.get("status"), STATUSES, f"results[{i}].status"),
                duration_ms=duration,
                metrics=_dict(item.get("metrics"), f"results[{i}].metrics"),
                message=message[:MAX_MESSAGE_CHARS] if message else None,
            )
        )
    return tuple(out)


def parse_json(payload: object) -> RunIn:
    if not isinstance(payload, dict):
        raise EvalInputError("최상위는 객체여야 합니다")
    started = _aware(payload.get("started_at"), "started_at", required=True)
    finished = _aware(payload.get("finished_at"), "finished_at", required=False)
    if finished is not None and finished < started:
        raise EvalInputError("finished_at 이 started_at 보다 이릅니다")
    target = payload.get("target", "")
    if not isinstance(target, str) or len(target) > 32:
        raise EvalInputError("target 은 32자 이하 문자열이어야 합니다")
    git_sha = payload.get("git_sha")
    if git_sha is not None and (not isinstance(git_sha, str) or len(git_sha) > 40):
        raise EvalInputError("git_sha 는 40자 이하 문자열이어야 합니다")
    note = payload.get("note", "")
    if not isinstance(note, str):
        raise EvalInputError("note 는 문자열이어야 합니다")
    return RunIn(
        kind=_choice(payload.get("kind"), KINDS, "kind"),
        environment=_choice(payload.get("environment"), ENVIRONMENTS, "environment"),
        data_kind=_choice(payload.get("data_kind"), DATA_KINDS, "data_kind"),
        started_at=started,
        finished_at=finished,
        target=target,
        git_sha=git_sha,
        summary=_dict(payload.get("summary"), "summary"),
        note=note,
        results=_results(payload.get("results", [])),
    )


def parse_junit(
    xml_text: str,
    *,
    kind: str,
    environment: str,
    data_kind: str,
    target: str = "",
    git_sha: str | None = None,
    note: str = "",
    now: datetime | None = None,
) -> RunIn:
    """JUnit XML(<testsuites> 또는 <testsuite>)을 실행 하나로. 시각은 기록 시점(now)과 총 소요 시간으로 정한다.

    XML 의 timestamp 는 도구마다 시간대 표기가 달라서(없거나 로컬 시각) 쓰지 않는다.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise EvalInputError(f"JUnit XML 이 아닙니다: {exc}") from exc
    cases = root.iter("testcase")
    results = []
    total_s = 0.0
    seen: set[str] = set()
    for case in cases:
        classname = case.get("classname") or ""
        base = f"{classname}.{case.get('name', '')}" if classname else case.get("name", "")
        name = base[:MAX_NAME_CHARS]
        n = 2
        while name in seen:  # 파라미터 테스트처럼 이름이 겹치면 번호를 붙인다
            name = f"{base[: MAX_NAME_CHARS - 6]}#{n}"
            n += 1
        seen.add(name)
        seconds = float(case.get("time") or 0)
        total_s += seconds
        status, message = "pass", None
        for tag, mapped in (("failure", "fail"), ("error", "error"), ("skipped", "skip")):
            node = case.find(tag)
            if node is not None:
                status = mapped
                message = (node.get("message") or node.text or "").strip()[:MAX_MESSAGE_CHARS] or None
                break
        results.append(ResultIn(name=name, status=status, duration_ms=round(seconds * 1000), message=message))
    if not results:
        raise EvalInputError("testcase 가 하나도 없습니다")
    finished = now or datetime.now(timezone.utc)
    counts = {s: sum(1 for r in results if r.status == s) for s in STATUSES}
    return RunIn(
        kind=_choice(kind, KINDS, "kind"),
        environment=_choice(environment, ENVIRONMENTS, "environment"),
        data_kind=_choice(data_kind, DATA_KINDS, "data_kind"),
        started_at=finished - timedelta(seconds=total_s),
        finished_at=finished,
        target=target,
        git_sha=git_sha,
        summary={"tests": len(results), **counts, "duration_s": round(total_s, 3)},
        note=note,
        results=tuple(results),
    )
