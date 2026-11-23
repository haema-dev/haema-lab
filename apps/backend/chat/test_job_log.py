"""metric=job 로그 한 줄의 형식. 저장하지 않은 Message 객체만 쓴다 (DB 없음)."""

from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from chat.models import Message
from chat.pipeline import log_job


class JobLogTests(SimpleTestCase):
    def line(self, **fields) -> str:
        fields.setdefault("created_at", timezone.now() - timedelta(seconds=42))
        with self.assertLogs("chat.pipeline", level="INFO") as logs:
            log_job(Message(pk=7, **fields))
        [line] = [m for m in logs.output if "metric=job" in m]
        return line

    def test_done_answer(self):
        line = self.line(status="done", source="generated", verification={"passed": True, "reasons": []})
        self.assertIn("metric=job message=7 status=done source=generated reason=- retries=0 e2e_s=42", line)

    def test_rejected_answer_lists_check_reasons(self):
        line = self.line(
            status="rejected",
            error="verification_failed",
            verification={"passed": False, "reasons": ["unsupported_fact", "truncated"]},
        )
        self.assertIn("status=rejected source=- reason=verification_failed,unsupported_fact,truncated", line)

    def test_failure_reason_keeps_only_the_code_not_details(self):
        line = self.line(status="failed", error="infra: /generate: HTTP 503: ollama down", retry_count=3)
        self.assertIn("reason=infra retries=3", line)
        self.assertNotIn("503", line)
