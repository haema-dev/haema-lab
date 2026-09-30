from __future__ import annotations

import http.client
import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock, skipUnless
from urllib.error import HTTPError, URLError

try:
    from django.test import Client, TestCase as DjangoTestCase, override_settings
except ModuleNotFoundError:
    Client = None
    DjangoTestCase = TestCase

    def override_settings(**_settings):
        return lambda target: target

from core.evaluation import (
    GoldenCase,
    load_golden_set,
    reciprocal_rank,
    recall_at_k,
    score_retrieval,
)
from core.ingest import (
    RENDER_VERSION,
    Document,
    IndexReport,
    InMemoryVectorIndex,
    content_hash,
    load_all_documents,
    load_documents,
    render_event,
    sync_index,
)
from core.ollama import (
    OllamaClient,
    OllamaConfig,
    OllamaConfigurationError,
    OllamaError,
    OllamaUnavailableError,
    format_query,
)
from core.rag import (
    DATA_DIRECTORY,
    DataNotFoundError,
    GenerationBusyError,
    GenerationGate,
    RetrievalResult,
    acquire_generation_slot,
    build_messages,
    fit_context,
    iter_ndjson,
    open_chat_stream,
    rank_by_keyword,
    retrieve,
    select_documents,
)
from core.verify import check_answer, extract_facts


class MonthlyDataTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_directory = Path(self.temporary_directory.name)
        self.month_file = self.data_directory / "2026-08.json"
        self.month_file.write_text(
            json.dumps(
                {
                    "observation_month": "2026-08",
                    "events": [
                        {
                            "event_id": "food-1",
                            "title": "양파 가격 상승",
                            "source_ids": ["source-1"],
                        }
                    ],
                    "sources": [{"source_id": "source-1", "title": "공식 가격 자료"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _add_july(self) -> None:
        (self.data_directory / "2026-07.json").write_text(
            json.dumps(
                {"observation_month": "2026-07", "events": [{"event_id": "july-1", "title": "7월 사건"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_month_in_question_filters_documents(self) -> None:
        self._add_july()
        scope, documents = select_documents("2026년 7월 사건은?", self.data_directory)
        self.assertEqual(scope, "2026-07")
        self.assertEqual([document.document_id for document in documents], ["july-1"])

    def test_question_without_month_searches_every_month(self) -> None:
        self._add_july()
        scope, documents = select_documents("가격이 오른 것은?", self.data_directory)
        self.assertEqual(scope, "all")
        self.assertEqual({document.document_id for document in documents}, {"july-1", "food-1"})

    def test_month_without_data_is_rejected_with_available_range(self) -> None:
        with self.assertRaisesRegex(DataNotFoundError, "2026-08 ~ 2026-08"):
            select_documents("2026년 7월 가격은?", self.data_directory)

    def test_loads_events_with_linked_sources(self) -> None:
        documents = load_documents(self.month_file)
        self.assertEqual(documents[0].document_id, "food-1")
        self.assertIn("공식 가격 자료", documents[0].text)

    def test_prompt_requires_grounded_answer(self) -> None:
        messages = build_messages(
            "2026년 8월에 식재료 중 가격 오른 것은?",
            RetrievalResult(scope="2026-08", documents=(), mode="empty"),
        )
        self.assertIn("추측하지 말고", messages[0]["content"])
        self.assertIn("검색된 문서 없음", messages[1]["content"])

    def test_prompt_labels_context_with_the_citation_format(self) -> None:
        messages = build_messages(
            "질문",
            RetrievalResult(scope="all", documents=(Document("food-1", "양파"),), mode="embedding"),
        )
        self.assertIn("대괄호", messages[0]["content"])
        self.assertIn("[food-1]\n양파", messages[1]["content"])
        self.assertNotIn("검색 방식", messages[1]["content"])

    def test_embedding_retrieval_is_not_tied_to_a_product_category(self) -> None:
        self.month_file.write_text(
            json.dumps(
                {
                    "observation_month": "2026-08",
                    "events": [
                        {"event_id": "rate", "title": "기준금리 인상"},
                        {"event_id": "material", "title": "철광석 원재료 가격 상승"},
                    ],
                    "sources": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        client = FakeEmbeddingClient({"기준금리": [0.0, 1.0], "원재료": [1.0, 0.0]})

        result = retrieve(
            "2026년 8월 원재료 가격은?",
            self.data_directory,
            top_k=1,
            client=client,
            index=InMemoryVectorIndex(),
        )

        self.assertEqual(result.mode, "embedding")
        self.assertEqual(result.documents[0].document_id, "material")

    def test_falls_back_to_keyword_when_embedding_fails(self) -> None:
        client = FakeEmbeddingClient({}, fail=True)

        result = retrieve(
            "2026년 8월 양파 가격", self.data_directory, client=client, index=InMemoryVectorIndex()
        )

        self.assertEqual(result.mode, "keyword-fallback")
        self.assertEqual(result.documents[0].document_id, "food-1")

    def test_unavailable_model_server_is_not_hidden_by_keyword_fallback(self) -> None:
        client = FakeEmbeddingClient({}, error=OllamaUnavailableError("runner crashed"))

        with self.assertRaises(OllamaUnavailableError):
            retrieve("2026년 8월 양파 가격", self.data_directory, client=client, index=InMemoryVectorIndex())

    def test_min_similarity_drops_unrelated_documents(self) -> None:
        self.month_file.write_text(
            json.dumps(
                {
                    "observation_month": "2026-08",
                    "events": [
                        {"event_id": "rate", "title": "기준금리 인상"},
                        {"event_id": "material", "title": "철광석 원재료 가격 상승"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        client = FakeEmbeddingClient({"기준금리": [0.0, 1.0], "원재료": [1.0, 0.0]})

        result = retrieve(
            "기준금리 알려줘",
            self.data_directory,
            client=client,
            index=InMemoryVectorIndex(),
            min_similarity=0.5,
        )

        self.assertEqual([document.document_id for document in result.documents], ["rate"])

    def test_keyword_ranking_drops_unrelated_documents(self) -> None:
        documents = [
            Document(document_id="rate", text="기준금리 인상"),
            Document(document_id="other", text="철광석 수입"),
        ]
        ranked = rank_by_keyword("기준금리 결정", documents)
        self.assertEqual([document.document_id for document in ranked], ["rate"])


class FitContextTests(TestCase):
    def test_keeps_whole_documents_in_rank_order_within_budget(self) -> None:
        documents = [Document("a", "x" * 6), Document("b", "y" * 6), Document("c", "z" * 3)]
        fitted = fit_context(documents, budget=10)
        self.assertEqual([document.document_id for document in fitted], ["a", "c"])
        self.assertEqual(fitted[0].text, "x" * 6)

    def test_truncates_only_an_oversized_top_document(self) -> None:
        fitted = fit_context([Document("a", "x" * 20), Document("b", "y")], budget=10)
        self.assertEqual([(d.document_id, d.text) for d in fitted], [("a", "x" * 10)])


class VerifyTests(TestCase):
    EVIDENCE = (
        Document(
            "evt_20260827_01",
            "2026-08-27 · 기준금리 인상\n결정: 한국은행 기준금리, 인상, 변동 +25bp, "
            "이전 금리 2.75%, 결정 후 금리 3.00%",
        ),
    )

    def test_equivalent_units_and_dates_normalize(self) -> None:
        self.assertEqual(
            extract_facts("0.25%p 올려 3% 로, 8월 27일"), {"25bp", "3.00%", "월:08", "일:08-27"}
        )
        self.assertEqual(
            extract_facts("2026-08-27 변동 +25bp, 3.00%"),
            {"25bp", "3.00%", "월:08", "월:2026-08", "일:08-27", "일:2026-08-27"},
        )

    def test_wrong_year_or_month_fails(self) -> None:
        wrong_year = check_answer("2025년 8월 27일 인상 [evt_20260827_01]", self.EVIDENCE)
        wrong_month = check_answer("7월에 3.00%로 인상 [evt_20260827_01]", self.EVIDENCE)
        self.assertEqual(wrong_year.unsupported_facts, ("월:2025-08", "일:2025-08-27"))
        self.assertEqual(wrong_month.unsupported_facts, ("월:07",))
        self.assertTrue(check_answer("2026년 8월에 인상 [evt_20260827_01]", self.EVIDENCE).passed)

    def test_bracketed_date_is_checked_as_a_date_not_a_citation(self) -> None:
        passed = check_answer("[2026-08-27] 3.00% [evt_20260827_01]", self.EVIDENCE)
        wrong = check_answer("[2026-07-30] 3.00% [evt_20260827_01]", self.EVIDENCE)
        self.assertTrue(passed.passed, passed)
        self.assertEqual(passed.citations, ("evt_20260827_01",))
        self.assertNotIn("unknown_citation", wrong.reasons)
        self.assertIn("일:2026-07-30", wrong.unsupported_facts)

    def test_faithful_answer_passes(self) -> None:
        result = check_answer(
            "8월 27일 기준금리를 2.75%에서 3%로 0.25%p 인상했습니다 [evt_20260827_01].", self.EVIDENCE
        )
        self.assertTrue(result.passed, result)
        self.assertEqual(result.citations, ("evt_20260827_01",))

    def test_citation_digits_are_not_treated_as_facts(self) -> None:
        self.assertTrue(check_answer("금리는 3.00%입니다 [evt_20260827_01]", self.EVIDENCE).passed)

    def test_number_missing_from_cited_document_fails(self) -> None:
        result = check_answer("금리는 3.25%입니다 [evt_20260827_01]", self.EVIDENCE)
        self.assertFalse(result.passed)
        self.assertEqual(result.unsupported_facts, ("3.25%",))

    def test_missing_or_unknown_citation_fails(self) -> None:
        self.assertEqual(check_answer("금리는 3.00%입니다.", self.EVIDENCE).reasons, ("no_citation", "unsupported_fact"))
        self.assertIn("unknown_citation", check_answer("3.00% [evt_x]", self.EVIDENCE).reasons)

    def test_refusal_needs_no_citation(self) -> None:
        self.assertTrue(check_answer("제공된 자료에서 확인할 수 없습니다.", self.EVIDENCE).passed)

    def test_restating_real_decision_lines_passes(self) -> None:
        """Guards against false positives on the real data format."""
        checked = 0
        for document in load_all_documents(DATA_DIRECTORY):
            for line in document.text.splitlines():
                if line.startswith("결정:") and extract_facts(line):
                    result = check_answer(f"{line} [{document.document_id}]", (document,))
                    self.assertTrue(result.passed, (document.document_id, result))
                    checked += 1
        self.assertGreater(checked, 0)


class FakeUpstream:
    def __init__(self, lines: list[bytes] | None = None) -> None:
        self.lines = lines or [b'{"done":true}\n']
        self.closed = False

    def __iter__(self):
        return iter(self.lines)

    def close(self) -> None:
        self.closed = True


class GenerationGateTests(TestCase):
    def setUp(self) -> None:
        gate_patch = mock.patch("core.rag._GATE", GenerationGate(1))
        gate_patch.start()
        self.addCleanup(gate_patch.stop)
        client = mock.Mock()
        client.open_chat_stream.side_effect = lambda messages: FakeUpstream()
        client_patch = mock.patch("core.rag._client", return_value=client)
        self.client = client_patch.start()
        self.addCleanup(client_patch.stop)

    def test_second_request_is_rejected_until_first_closes(self) -> None:
        first = open_chat_stream([], acquire_generation_slot())
        with self.assertRaises(GenerationBusyError):
            acquire_generation_slot()
        first.close()
        open_chat_stream([], acquire_generation_slot()).close()

    def test_double_close_and_release_free_the_slot_once(self) -> None:
        slot = acquire_generation_slot()
        stream = open_chat_stream([], slot)
        stream.close()
        stream.close()
        slot.release()  # BoundedSemaphore would raise ValueError on over-release

    def test_slot_is_freed_when_upstream_fails_to_open(self) -> None:
        self.client.return_value.open_chat_stream.side_effect = OllamaError("down")
        with self.assertRaises(OllamaError):
            open_chat_stream([], acquire_generation_slot())
        self.client.return_value.open_chat_stream.side_effect = lambda messages: FakeUpstream()
        open_chat_stream([], acquire_generation_slot()).close()

    def test_ndjson_stream_closes_upstream_without_iteration(self) -> None:
        upstream = FakeUpstream()
        iter_ndjson(upstream).close()
        self.assertTrue(upstream.closed)


def _trailer(stream_lines: list[bytes]) -> dict:
    return json.loads(stream_lines[-1])["rag"]


class NdjsonTrailerTests(TestCase):
    DOCUMENTS = (Document("evt_1", "결정 후 금리 3.00%"),)

    def test_verification_is_appended_after_done(self) -> None:
        upstream = FakeUpstream(
            [
                b'{"message":{"content":"3.00%"},"done":false}\n',
                b'{"message":{"content":" [evt_1]"},"done":false}\n',
                b'{"done":true,"done_reason":"stop"}\n',
            ]
        )
        lines = list(iter_ndjson(upstream, self.DOCUMENTS))
        self.assertEqual(len(lines), 4)
        self.assertTrue(_trailer(lines)["verification"]["passed"])
        self.assertTrue(upstream.closed)

    def test_trailer_reports_measured_token_counts(self) -> None:
        upstream = FakeUpstream(
            [b'{"message":{"content":"x"},"done":false}\n', b'{"done":true,"prompt_eval_count":812,"eval_count":64}\n']
        )
        usage = _trailer(list(iter_ndjson(upstream)))["usage"]
        self.assertEqual(usage, {"prompt_tokens": 812, "output_tokens": 64})

    def test_answer_cut_by_num_predict_is_not_verified(self) -> None:
        upstream = FakeUpstream(
            [b'{"message":{"content":"3.00% [evt_1]"},"done":false}\n', b'{"done":true,"done_reason":"length"}\n']
        )
        verification = _trailer(list(iter_ndjson(upstream, self.DOCUMENTS)))["verification"]
        self.assertFalse(verification["passed"])
        self.assertIn("truncated", verification["reasons"])

    def test_stream_without_done_reports_interruption(self) -> None:
        upstream = FakeUpstream([b'{"message":{"content":"3.00"},"done":false}\n'])
        self.assertEqual(_trailer(list(iter_ndjson(upstream))), {"error": "stream_interrupted"})

    def test_connection_reset_mid_stream_reports_interruption(self) -> None:
        class Broken(FakeUpstream):
            def __iter__(self):
                yield b'{"message":{"content":"3"},"done":false}\n'
                raise ConnectionResetError

        upstream = Broken()
        lines = list(iter_ndjson(upstream))
        self.assertEqual(_trailer(lines), {"error": "stream_interrupted"})
        self.assertTrue(upstream.closed)


class FakeEmbeddingClient:
    """Deterministic embedder: a text gets the vector of the first keyword it contains."""

    def __init__(
        self, vectors: dict[str, list[float]], fail: bool = False, error: Exception | None = None
    ) -> None:
        self.config = OllamaConfig(base_url="http://fake", chat_model="chat", embedding_model="embed-v1")
        self.vectors = vectors
        self.error = error or (OllamaError("down") if fail else None)
        self.embedded_texts: list[str] = []
        self.queries: list[str] = []

    def _vector(self, text: str) -> list[float]:
        return next((v for key, v in self.vectors.items() if key in text), [0.5, 0.5])

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.error:
            raise self.error
        self.embedded_texts.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return self._vector(query)


def _document(document_id: str, sha: str) -> Document:
    return Document(document_id=document_id, text=f"text {document_id}", content_sha256=sha)


class IngestTests(TestCase):
    def test_sync_is_idempotent(self) -> None:
        index = InMemoryVectorIndex()
        client = FakeEmbeddingClient({})
        documents = [_document("a", "h1"), _document("b", "h2")]

        first = sync_index(documents, index, client.embed, "embed-v1")
        second = sync_index(documents, index, client.embed, "embed-v1")

        self.assertEqual(first, IndexReport(inserted=2, updated=0, unchanged=0))
        self.assertEqual(second, IndexReport(inserted=0, updated=0, unchanged=2))
        self.assertEqual(len(index), 2)
        self.assertEqual(len(client.embedded_texts), 2)

    def test_sync_reembeds_only_changed_content(self) -> None:
        index = InMemoryVectorIndex()
        client = FakeEmbeddingClient({})
        sync_index([_document("a", "h1"), _document("b", "h2")], index, client.embed, "embed-v1")

        report = sync_index(
            [_document("a", "h1"), _document("b", "changed")], index, client.embed, "embed-v1"
        )

        self.assertEqual(report, IndexReport(inserted=0, updated=1, unchanged=1))
        self.assertEqual(index.get("b", "embed-v1").content_sha256, "changed")

    def test_new_embedding_model_is_indexed_separately(self) -> None:
        index = InMemoryVectorIndex()
        client = FakeEmbeddingClient({})
        sync_index([_document("a", "h1")], index, client.embed, "embed-v1")

        report = sync_index([_document("a", "h1")], index, client.embed, "embed-v2")

        self.assertEqual(report.inserted, 1)
        self.assertEqual(len(index), 2)

    def test_content_hash_ignores_key_order(self) -> None:
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))

    def test_render_event_includes_decision_numbers(self) -> None:
        text = render_event(
            {
                "event_date": "2026-08-27",
                "title": "기준금리 인상",
                "decision": {
                    "policy_instrument": "한국은행 기준금리",
                    "action_ko": "인상",
                    "change_basis_points": 25,
                    "target_rate_percent": 3.0,
                },
            },
            [{"institution": "한국은행", "title": "보도자료", "published_at": "2026-08-27"}],
        )
        self.assertIn("2026-08-27 · 기준금리 인상", text)
        self.assertNotIn("[2026-08-27]", text)
        self.assertIn("변동 +25bp", text)
        self.assertIn("결정 후 금리 3.00%", text)
        self.assertIn("출처: 한국은행 보도자료 (2026-08-27)", text)

    def test_render_version_is_part_of_the_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-08.json"
            path.write_text(json.dumps({"events": [{"event_id": "e1", "title": "t"}]}), encoding="utf-8")
            current = load_documents(path)[0].content_sha256
            with mock.patch("core.ingest.RENDER_VERSION", RENDER_VERSION + 1):
                bumped = load_documents(path)[0].content_sha256
        self.assertNotEqual(current, bumped)

    def test_real_data_files_load(self) -> None:
        documents = load_all_documents(DATA_DIRECTORY)
        ids = [document.document_id for document in documents]
        self.assertTrue(documents)
        self.assertEqual(len(ids), len(set(ids)))


class OllamaClientTests(TestCase):
    def setUp(self) -> None:
        self.config = OllamaConfig(
            base_url="http://fake/", chat_model="qwen", embedding_model="embed", num_ctx=4096
        )

    def test_query_instruction_prefix(self) -> None:
        self.assertEqual(format_query("Find docs", "금리"), "Instruct: Find docs\nQuery: 금리")
        self.assertEqual(format_query("", "금리"), "금리")

    def test_embed_query_sends_instruction_but_documents_do_not(self) -> None:
        client = OllamaClient(self.config)
        with mock.patch.object(client, "_call_json", return_value={"embeddings": [[1.0]]}) as call:
            client.embed_query("금리")
            client.embed(["문서"])
        query_input = call.call_args_list[0].args[1]["input"]
        document_input = call.call_args_list[1].args[1]["input"]
        self.assertTrue(query_input[0].startswith("Instruct: "))
        self.assertEqual(document_input, ["문서"])

    def test_chat_payload_pins_context_window_and_think(self) -> None:
        payload = OllamaClient(self.config).chat_payload([{"role": "user", "content": "hi"}])
        self.assertEqual(payload["options"], {"num_ctx": 4096, "num_predict": 256})
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["keep_alive"], "30m")

    def test_embed_on_cpu_disables_gpu_layers(self) -> None:
        config = OllamaConfig(base_url="http://x", chat_model="q", embedding_model="e", embed_on_cpu=True)
        client = OllamaClient(config)
        with mock.patch.object(client, "_call_json", return_value={"embeddings": [[1.0]]}) as call:
            client.embed(["문서"])
        self.assertEqual(call.call_args.args[1]["options"], {"num_gpu": 0})

    def test_server_failures_are_retryable_and_client_errors_are_not(self) -> None:
        client = OllamaClient(self.config)
        for exc, expected in (
            (HTTPError("http://fake", 500, "runner crashed", {}, None), OllamaUnavailableError),
            (URLError("connection refused"), OllamaUnavailableError),
            (TimeoutError(), OllamaUnavailableError),
        ):
            with mock.patch("core.ollama.urlopen", side_effect=exc):
                with self.assertRaises(expected):
                    client.open_chat_stream([])
        with mock.patch("core.ollama.urlopen", side_effect=HTTPError("http://fake", 400, "bad", {}, None)):
            with self.assertRaises(OllamaError) as caught:
                client.embed(["문서"])
        self.assertNotIsInstance(caught.exception, OllamaUnavailableError)

    def test_connection_lost_while_reading_is_retryable(self) -> None:
        client = OllamaClient(self.config)
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = ConnectionResetError
        with mock.patch("core.ollama.urlopen", return_value=response):
            with self.assertRaises(OllamaUnavailableError):
                client.embed(["문서"])
        with mock.patch("core.ollama.urlopen", side_effect=http.client.RemoteDisconnected("closed")):
            with self.assertRaises(OllamaUnavailableError):
                client.open_chat_stream([])
        with mock.patch("core.ollama.urlopen", side_effect=http.client.IncompleteRead(b"")):
            with self.assertRaises(OllamaUnavailableError):
                client.embed(["문서"])

    def test_non_positive_num_predict_is_rejected(self) -> None:
        with self.assertRaises(OllamaConfigurationError):
            OllamaConfig(base_url="http://x", chat_model="q", embedding_model="e", num_predict=0)

    def test_missing_setting_is_reported(self) -> None:
        with self.assertRaises(OllamaConfigurationError):
            OllamaConfig(base_url="", chat_model="qwen", embedding_model="embed")


class EvaluationTests(TestCase):
    def test_recall_and_reciprocal_rank(self) -> None:
        relevant = frozenset({"b", "z"})
        self.assertEqual(recall_at_k(["a", "b", "c"], relevant, 2), 0.5)
        self.assertEqual(reciprocal_rank(["a", "b"], relevant), 0.5)
        self.assertEqual(reciprocal_rank(["a"], relevant), 0.0)

    def test_score_retrieval_reports_misses(self) -> None:
        cases = (
            GoldenCase("hit", "q1", "numeric", frozenset({"a"})),
            GoldenCase("miss", "q2", "context", frozenset({"z"})),
        )
        score = score_retrieval(cases, lambda question: ["a", "b"], k=5)
        self.assertEqual(score.recall_at_k, 0.5)
        self.assertEqual(score.mrr, 0.5)
        self.assertEqual(score.misses, ("miss",))

    def test_golden_set_references_existing_documents(self) -> None:
        cases = load_golden_set(DATA_DIRECTORY.parent / "eval" / "golden_retrieval.jsonl")
        known_ids = {document.document_id for document in load_all_documents(DATA_DIRECTORY)}
        for case in cases:
            self.assertLessEqual(case.relevant_ids, known_ids, case.case_id)


@skipUnless(Client is not None, "Django is not installed in this Python environment")
class RagChatViewTests(DjangoTestCase):
    def setUp(self) -> None:
        self.client = Client()
        # No view test may reach a real inference node or the process-wide gate.
        self.slot = mock.Mock()
        slot_patch = mock.patch("core.views.acquire_generation_slot", return_value=self.slot)
        self.acquire = slot_patch.start()
        self.addCleanup(slot_patch.stop)

    def test_requires_question(self) -> None:
        response = self.client.post(
            "/api/rag/chat", data=json.dumps({}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(RAG_MAX_QUESTION_CHARS=300)
    def test_rejects_question_over_limit(self) -> None:
        response = self.client.post(
            "/api/rag/chat",
            data=json.dumps({"question": "가" * 301}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("300자", response.json()["error"])

    @override_settings(RAG_MAX_QUESTION_CHARS=300)
    @mock.patch("core.views.retrieve", side_effect=DataNotFoundError("no data"))
    def test_body_limit_fits_escaped_max_question(self, retrieve_mock) -> None:
        response = self.client.post(
            "/api/rag/chat",
            data=json.dumps({"question": "가" * 300}),  # ensure_ascii escapes to 6 bytes/char
            content_type="application/json",
        )
        self.assertNotEqual(response.status_code, 413)
        retrieve_mock.assert_called_once()

    @mock.patch("core.views.open_chat_stream")
    @mock.patch("core.views.retrieve")
    def test_unavailable_model_server_returns_503(self, retrieve_mock, stream_mock) -> None:
        retrieve_mock.return_value = RetrievalResult(scope="all", documents=(), mode="empty")
        stream_mock.side_effect = OllamaUnavailableError("down")

        response = self.client.post(
            "/api/rag/chat",
            data=json.dumps({"question": "기준금리는?"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "30")
        self.slot.release.assert_called()

    @mock.patch("core.views.retrieve")
    def test_busy_request_is_rejected_before_any_gpu_work(self, retrieve_mock) -> None:
        self.acquire.side_effect = GenerationBusyError("busy")

        response = self.client.post(
            "/api/rag/chat",
            data=json.dumps({"question": "기준금리는?"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "30")
        retrieve_mock.assert_not_called()

    @mock.patch("core.views.retrieve", side_effect=OllamaUnavailableError("runner crashed"))
    def test_slot_is_released_when_retrieval_fails(self, retrieve_mock) -> None:
        response = self.client.post(
            "/api/rag/chat",
            data=json.dumps({"question": "기준금리는?"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.slot.release.assert_called_once()

    @mock.patch("core.views.retrieve")
    def test_accepts_post_without_csrf_token(self, retrieve_mock) -> None:
        retrieve_mock.side_effect = DataNotFoundError("no data")
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/api/rag/chat",
            data=json.dumps({"question": "기준금리는?"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    @mock.patch("core.views.open_chat_stream")
    @mock.patch("core.views.retrieve")
    def test_streams_upstream_ndjson(self, retrieve_mock, stream_mock) -> None:
        class FakeResponse:
            closed = False

            def __iter__(self):
                return iter([b'{"message":{"content":"answer"},"done":false}\n', b'{"done":true}\n'])

            def close(self):
                self.closed = True

        upstream = FakeResponse()
        retrieve_mock.return_value = RetrievalResult(
            scope="2026-08", documents=(), mode="keyword-fallback"
        )
        stream_mock.return_value = upstream

        response = self.client.post(
            "/api/rag/chat",
            data=json.dumps({"question": "2026년 8월에 식재료 중 가격 오른 것은?"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-RAG-Scope"], "2026-08")
        lines = b"".join(response.streaming_content).splitlines()
        self.assertEqual(lines[0], b'{"message":{"content":"answer"},"done":false}')
        self.assertIn("verification", json.loads(lines[-1])["rag"])
        self.assertTrue(upstream.closed)
