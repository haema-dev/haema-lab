"""Retrieval ablation over the golden set: Recall@k and MRR per search mode.

Usage (from apps/backend):
    python -m scripts.eval_retrieval --modes keyword            # no Ollama needed
    python -m scripts.eval_retrieval --modes keyword embed embed-noinstr

``embed`` uses the Qwen3-Embedding query instruction; ``embed-noinstr`` sends the
raw question so the effect of the prefix shows up as a separate row.
Searches every monthly file, independent of the API's one-month window.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from core.evaluation import GoldenSetError, load_golden_set, score_retrieval
from core.ingest import Document, InMemoryVectorIndex, load_all_documents
from core.ollama import OllamaClient, OllamaConfig, OllamaError
from core.rag import rank_by_keyword, vector_search

BACKEND_DIRECTORY = Path(__file__).resolve().parent.parent
MODES = ("keyword", "embed", "embed-noinstr")


def make_ranker(
    mode: str, documents: Sequence[Document], index: InMemoryVectorIndex
) -> Callable[[str], list[str]]:
    """Return question -> ranked document ids for the given search mode."""
    if mode == "keyword":
        return lambda question: [d.document_id for d in rank_by_keyword(question, documents)]

    config = OllamaConfig.from_env()
    if mode == "embed-noinstr":
        config = dataclasses.replace(config, query_instruction="")
    client = OllamaClient(config)
    # Both embed modes share one index: document vectors are identical, only queries differ.
    return lambda question: [d.document_id for d in vector_search(question, documents, client, index)]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", type=Path, default=BACKEND_DIRECTORY / "eval" / "golden_retrieval.jsonl")
    parser.add_argument("--data", type=Path, default=BACKEND_DIRECTORY / "data")
    parser.add_argument("--modes", nargs="+", choices=MODES, default=["keyword"])
    parser.add_argument("-k", type=int, default=5)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cases = load_golden_set(args.golden)
    except (OSError, GoldenSetError) as exc:
        print(f"골든셋 오류: {exc}", file=sys.stderr)
        return 2
    documents = load_all_documents(args.data)
    index = InMemoryVectorIndex()

    print(f"cases={len(cases)} documents={len(documents)}\n")
    print(f"| mode | Recall@{args.k} | MRR | misses |")
    print("|---|---|---|---|")
    for mode in args.modes:
        try:
            score = score_retrieval(cases, make_ranker(mode, documents, index), k=args.k)
        except OllamaError as exc:
            print(f"| {mode} | - | - | 실패: {exc} |")
            continue
        misses = ", ".join(score.misses) or "-"
        print(f"| {mode} | {score.recall_at_k:.3f} | {score.mrr:.3f} | {misses} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
