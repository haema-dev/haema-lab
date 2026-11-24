"""테스트·평가 결과를 eval_run / eval_result 에 기록한다. 환경과 데이터 종류는 반드시 밝힌다.

  python manage.py record_eval --json result.json
  python manage.py record_eval --junit report.xml --kind unit --environment ci --data-kind mock --target ai

git_sha 를 주지 않으면 환경변수 GITHUB_SHA(CI)를 쓴다. 검증에 실패하면 아무것도 저장하지 않는다.
"""

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from chat.eval_record import DATA_KINDS, ENVIRONMENTS, KINDS, EvalInputError, RunIn, parse_json, parse_junit
from chat.models import EvalResult, EvalRun


def save_run(run: RunIn) -> EvalRun:
    with transaction.atomic():
        row = EvalRun.objects.create(
            kind=run.kind,
            environment=run.environment,
            data_kind=run.data_kind,
            target=run.target,
            git_sha=run.git_sha,
            started_at=run.started_at,
            finished_at=run.finished_at,
            passed=run.passed,
            summary=run.summary,
            note=run.note,
        )
        EvalResult.objects.bulk_create(
            EvalResult(
                run=row,
                name=r.name,
                status=r.status,
                duration_ms=r.duration_ms,
                metrics=r.metrics,
                message=r.message,
            )
            for r in run.results
        )
    return row


class Command(BaseCommand):
    help = "Record a test/eval run (environment and data kind are required)."

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--json", type=Path, help="실행 하나를 담은 JSON 파일")
        source.add_argument("--junit", type=Path, help="JUnit XML (pytest --junitxml, Gradle test-results)")
        parser.add_argument("--kind", choices=KINDS, help="--junit 일 때 필수")
        parser.add_argument("--environment", choices=ENVIRONMENTS, help="--junit 일 때 필수")
        parser.add_argument("--data-kind", choices=DATA_KINDS, help="--junit 일 때 필수")
        parser.add_argument("--target", default="")
        parser.add_argument("--git-sha", default=None)
        parser.add_argument("--note", default="")

    def handle(self, *args, json: Path | None, junit: Path | None, **options):
        git_sha = options["git_sha"] or os.environ.get("GITHUB_SHA") or None
        path = json or junit
        if not path.exists():
            raise CommandError(f"파일이 없습니다: {path}")
        text = path.read_text(encoding="utf-8")
        try:
            if json is not None:
                payload = _load_json(text)
                if git_sha and not payload.get("git_sha"):
                    payload["git_sha"] = git_sha
                run = parse_json(payload)
            else:
                missing = [name for name in ("kind", "environment", "data_kind") if not options[name]]
                if missing:
                    raise CommandError(f"--junit 에는 {', '.join('--' + m.replace('_', '-') for m in missing)} 가 필요합니다")
                run = parse_junit(
                    text,
                    kind=options["kind"],
                    environment=options["environment"],
                    data_kind=options["data_kind"],
                    target=options["target"],
                    git_sha=git_sha,
                    note=options["note"],
                )
        except EvalInputError as exc:
            raise CommandError(str(exc)) from exc
        row = save_run(run)
        self.stdout.write(
            f"eval_run id={row.pk} kind={run.kind} environment={run.environment} data_kind={run.data_kind} "
            f"passed={run.passed} results={len(run.results)}"
        )


def _load_json(text: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvalInputError(f"JSON 형식 오류: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvalInputError("최상위는 객체여야 합니다")
    return payload
