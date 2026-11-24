"""record_eval 이 실제로 eval_run / eval_result 에 저장하는지 (PostgreSQL 필요, CI)."""

import json
import tempfile
from pathlib import Path

from django.core.management import call_command

from chat._testing import ChatTestCase
from chat.models import EvalResult, EvalRun
from chat.test_eval_record import JUNIT, RUN


class RecordEvalDbTests(ChatTestCase):
    def write(self, text, suffix):
        path = Path(tempfile.mkdtemp()) / f"r{suffix}"
        path.write_text(text, encoding="utf-8")
        return path

    def test_json_run_and_results_are_saved_with_environment(self):
        call_command("record_eval", "--json", str(self.write(json.dumps(RUN), ".json")))
        run = EvalRun.objects.get()
        self.assertEqual((run.kind, run.environment, run.data_kind, run.passed), ("cycle", "server", "real", True))
        self.assertEqual(run.summary, {"e2e_s": 21.5})
        self.assertEqual(list(run.results.values_list("name", "status")), [("generate", "pass")])

    def test_junit_run_is_saved(self):
        call_command("record_eval", "--junit", str(self.write(JUNIT, ".xml")),
                     "--kind", "unit", "--environment", "ci", "--data-kind", "mock", "--target", "ai")
        run = EvalRun.objects.get()
        self.assertEqual((run.environment, run.data_kind, run.passed), ("ci", "mock", False))
        self.assertEqual(EvalResult.objects.filter(run=run).count(), 4)
