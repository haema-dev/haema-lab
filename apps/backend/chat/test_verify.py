from django.test import SimpleTestCase

from chat.verify import Document, check_answer, extract_facts

DOC = Document("12", "2026-08-27 기준금리를 2.50%로 동결했다. 변동 0bp.")


class VerifyTests(SimpleTestCase):
    def test_supported_answer_passes(self):
        result = check_answer("2026년 8월 27일 기준금리는 2.50%로 동결됐다. [12]", [DOC])
        self.assertTrue(result.passed, result.reasons)
        self.assertEqual(result.citations, ("12",))

    def test_wrong_number_is_unsupported(self):
        result = check_answer("기준금리는 3.00%로 동결됐다. [12]", [DOC])
        self.assertIn("unsupported_fact", result.reasons)

    def test_unknown_or_missing_citation_fails(self):
        self.assertIn("unknown_citation", check_answer("동결됐다. [99]", [DOC]).reasons)
        self.assertIn("no_citation", check_answer("동결됐다.", [DOC]).reasons)

    def test_refusal_needs_no_citation(self):
        self.assertTrue(check_answer("제공된 자료에서 확인할 수 없습니다", []).passed)

    def test_bp_and_percent_point_are_equal(self):
        self.assertEqual(extract_facts("0.25%p"), extract_facts("25bp"))
