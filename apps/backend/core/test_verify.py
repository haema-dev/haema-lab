from django.test import SimpleTestCase

from core.verify import Document, check_answer, check_generation, extract_facts

DOC = Document("12", "2026-08-27 기준금리를 2.50%로 동결했다. 변동 0bp.")
GOOD = "2026년 8월 27일 기준금리는 2.50%로 동결됐다. [12]"


class CheckAnswerTests(SimpleTestCase):
    def test_supported_answer_passes(self):
        result = check_answer(GOOD, [DOC])
        self.assertTrue(result.passed, result.reasons)
        self.assertEqual(result.citations, ("12",))

    def test_wrong_number_is_unsupported(self):
        result = check_answer("기준금리는 3.00%로 동결됐다. [12]", [DOC])
        self.assertEqual(result.reasons, ("unsupported_fact",))
        self.assertEqual(result.unsupported_facts, ("3.00%",))

    def test_wrong_year_is_unsupported(self):
        result = check_answer("2025년 8월 27일 기준금리는 2.50%였다. [12]", [DOC])
        self.assertIn("unsupported_fact", result.reasons)

    def test_unknown_or_missing_citation_fails(self):
        self.assertIn("unknown_citation", check_answer("동결됐다. [99]", [DOC]).reasons)
        self.assertIn("no_citation", check_answer("동결됐다.", [DOC]).reasons)

    def test_facts_are_checked_only_against_cited_documents(self):
        other = Document("13", "2026-07-10 기준금리 3.00%.")
        result = check_answer("기준금리는 3.00%였다. [12]", [DOC, other])
        self.assertIn("unsupported_fact", result.reasons)

    def test_bracketed_iso_date_is_not_a_citation(self):
        result = check_answer("[2026-08-27] 2.50% 동결. [12]", [DOC])
        self.assertEqual(result.citations, ("12",))
        self.assertTrue(result.passed, result.reasons)

    def test_digits_in_citation_ids_are_not_facts(self):
        doc = Document("evt_20260827_01", "2026-08-27 기준금리 2.50%.")
        self.assertTrue(check_answer("2.50%로 동결. [evt_20260827_01]", [doc]).passed)

    def test_refusal_needs_no_citation(self):
        self.assertTrue(check_answer("제공된 자료에서 확인할 수 없습니다", []).passed)


class ExtractFactsTests(SimpleTestCase):
    def test_bp_and_percent_point_are_equal(self):
        self.assertEqual(extract_facts("0.25%p"), extract_facts("25bp"))

    def test_percent_is_normalized(self):
        self.assertEqual(extract_facts("3%"), extract_facts("3.00 퍼센트"))

    def test_iso_date_supports_korean_date_forms(self):
        self.assertLessEqual(extract_facts("8월 27일"), extract_facts("2026-08-27"))
        self.assertLessEqual(extract_facts("2026년 8월"), extract_facts("2026-08-27"))


class CheckGenerationTests(SimpleTestCase):
    def test_complete_answer_is_the_same_as_check_answer(self):
        self.assertEqual(check_generation(GOOD, [DOC], truncated=False), check_answer(GOOD, [DOC]))

    def test_truncated_answer_fails_even_when_facts_match(self):
        result = check_generation(GOOD, [DOC], truncated=True)
        self.assertFalse(result.passed)
        self.assertEqual(result.reasons, ("truncated",))
