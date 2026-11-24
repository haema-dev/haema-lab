"""평가 결과 기록: 입력 검증·변환(DB 없이)과 명령 인자. 실제 저장은 test_eval_record_db (CI, PostgreSQL)."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from chat.eval_record import EvalInputError, parse_json, parse_junit

RUN = {
    "kind": "cycle", "environment": "server", "data_kind": "real", "target": "cluster",
    "started_at": "2026-11-24T10:00:00+09:00", "finished_at": "2026-11-24T10:00:21.5+09:00",
    "summary": {"e2e_s": 21.5},
    "results": [{"name": "generate", "status": "pass", "duration_ms": 21500, "metrics": {"cited": [1]}}],
}

JUNIT = """<testsuites><testsuite name="s" tests="4">
  <testcase classname="tests.test_api" name="test_ok" time="0.10"/>
  <testcase classname="tests.test_api" name="test_bad" time="0.20"><failure message="assert 1 == 2">trace</failure></testcase>
  <testcase classname="tests.test_api" name="test_skip" time="0"><skipped message="no gpu"/></testcase>
  <testcase classname="tests.test_api" name="test_ok" time="0.05"/>
</testsuite></testsuites>"""


class ParseJsonTests(SimpleTestCase):
    def test_valid_run(self):
        run = parse_json(RUN)
        self.assertEqual((run.kind, run.environment, run.data_kind, run.passed), ("cycle", "server", "real", True))
        self.assertEqual(run.results[0].duration_ms, 21500)

    def test_environment_and_data_kind_are_required(self):
        for key in ("environment", "data_kind", "kind"):
            with self.subTest(key=key), self.assertRaises(EvalInputError):
                parse_json({k: v for k, v in RUN.items() if k != key})

    def test_unknown_environment_is_rejected(self):
        with self.assertRaises(EvalInputError):
            parse_json({**RUN, "environment": "prod"})

    def test_naive_time_is_rejected(self):
        with self.assertRaises(EvalInputError):
            parse_json({**RUN, "started_at": "2026-11-24T10:00:00"})

    def test_finish_before_start_is_rejected(self):
        with self.assertRaises(EvalInputError):
            parse_json({**RUN, "finished_at": "2026-11-24T09:00:00+09:00"})

    def test_duplicate_names_and_bad_status_are_rejected(self):
        dup = {**RUN, "results": RUN["results"] * 2}
        bad = {**RUN, "results": [{"name": "x", "status": "ok"}]}
        for payload in (dup, bad):
            with self.subTest(), self.assertRaises(EvalInputError):
                parse_json(payload)

    def test_passed_ignores_skips_but_needs_at_least_one_case(self):
        skipped = {**RUN, "results": [{"name": "a", "status": "pass"}, {"name": "b", "status": "skip"}]}
        self.assertTrue(parse_json(skipped).passed)
        self.assertFalse(parse_json({**RUN, "results": []}).passed)
        self.assertFalse(parse_json({**RUN, "results": [{"name": "a", "status": "error"}]}).passed)


class ParseJunitTests(SimpleTestCase):
    def test_statuses_names_and_summary(self):
        now = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)
        run = parse_junit(JUNIT, kind="unit", environment="local", data_kind="mock", target="ai", now=now)
        self.assertEqual([r.status for r in run.results], ["pass", "fail", "skip", "pass"])
        self.assertEqual(run.results[3].name, "tests.test_api.test_ok#2")  # 겹치는 이름에 번호
        self.assertEqual(run.results[1].message, "assert 1 == 2")
        self.assertEqual(run.summary, {"tests": 4, "pass": 2, "fail": 1, "skip": 1, "error": 0, "duration_s": 0.35})
        self.assertFalse(run.passed)
        self.assertEqual(run.finished_at, now)
        self.assertEqual((now - run.started_at).total_seconds(), 0.35)

    def test_empty_or_broken_xml_is_rejected(self):
        for text in ("<testsuites/>", "not xml"):
            with self.subTest(text=text), self.assertRaises(EvalInputError):
                parse_junit(text, kind="unit", environment="ci", data_kind="mock")


class RecordEvalCommandTests(SimpleTestCase):
    def write(self, text, suffix):
        path = Path(tempfile.mkdtemp()) / f"r{suffix}"
        path.write_text(text, encoding="utf-8")
        return path

    def test_junit_requires_environment_and_data_kind(self):
        path = self.write(JUNIT, ".xml")
        with patch("chat.management.commands.record_eval.save_run") as save, self.assertRaises(CommandError):
            call_command("record_eval", "--junit", str(path), "--kind", "unit")
        save.assert_not_called()

    def test_invalid_json_saves_nothing(self):
        path = self.write(json.dumps({**RUN, "environment": "?"}), ".json")
        with patch("chat.management.commands.record_eval.save_run") as save, self.assertRaises(CommandError):
            call_command("record_eval", "--json", str(path))
        save.assert_not_called()

    def test_git_sha_falls_back_to_github_sha(self):
        path = self.write(json.dumps(RUN), ".json")
        with patch("chat.management.commands.record_eval.save_run") as save, \
                patch.dict("os.environ", {"GITHUB_SHA": "abc123"}):
            save.return_value.pk = 1
            call_command("record_eval", "--json", str(path))
        self.assertEqual(save.call_args.args[0].git_sha, "abc123")
