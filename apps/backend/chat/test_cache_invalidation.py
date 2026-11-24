"""캐시 무효화 쿼리 모양. DB 없이 AnswerCache 를 mock 으로 바꿔 확인한다 (실제 삭제는 test_worker.LoadChunksTests, CI)."""

from unittest.mock import patch

from django.test import SimpleTestCase

from chat.retrieval import invalidate_cache_for_chunks


class InvalidateCacheTests(SimpleTestCase):
    def test_deletes_answers_citing_any_changed_chunk(self):
        with patch("chat.retrieval.AnswerCache") as model:
            model.objects.filter.return_value.delete.return_value = (2, {})
            self.assertEqual(invalidate_cache_for_chunks([3, 7]), 2)
        model.objects.filter.assert_called_once_with(cited_chunk_ids__overlap=[3, 7])

    def test_nothing_changed_means_no_query(self):
        with patch("chat.retrieval.AnswerCache") as model:
            self.assertEqual(invalidate_cache_for_chunks([]), 0)
        model.objects.filter.assert_not_called()
