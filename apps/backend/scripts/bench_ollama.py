"""Measure Ollama chat/embedding latency from this machine.

Usage (from apps/backend, with OLLAMA_* env vars set):
    python -m scripts.bench_ollama --runs 5 --think both

Reports per scenario: TTFT (client-side), total time, prompt-eval tok/s and
generation tok/s (server-side, from Ollama's final stream chunk). The first
``--warmup`` runs are discarded because they include model load time.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from core.ingest import load_all_documents
from core.ollama import OllamaClient, OllamaConfig, OllamaError

DATA_DIRECTORY = Path(__file__).resolve().parent.parent / "data"
NS_PER_SECOND = 1e9
SHORT_QUESTION = "기준금리가 환율에 영향을 주는 경로를 3문장으로 설명해줘."
# Roughly 3k tokens of Korean context, similar to a RAG prompt with several documents.
LONG_PROMPT_CHARS = 6000


@dataclass(frozen=True)
class RunStats:
    ttft: float
    total: float
    load: float
    prompt_tokens: int
    prompt_tps: float
    output_tokens: int
    output_tps: float


def build_long_prompt(data_directory: Path, target_chars: int) -> str:
    """Repeat the real documents until the context is about ``target_chars`` long."""
    texts = [doc.text for doc in load_all_documents(data_directory)] or [SHORT_QUESTION]
    context: list[str] = []
    while sum(len(t) for t in context) < target_chars:
        context.extend(texts)
    return "컨텍스트:\n" + "\n\n".join(context) + "\n\n질문: 위 자료에서 한국은행 기준금리 변화만 요약해줘."


def _rate(count: int, duration_ns: int) -> float:
    return count / (duration_ns / NS_PER_SECOND) if duration_ns else 0.0


def run_once(client: OllamaClient, prompt: str, think: bool) -> RunStats:
    """Stream one chat request and collect client- and server-side timings.

    A unique tag goes at the very start of the prompt: Ollama reuses the KV cache
    for a shared prompt prefix, which would otherwise make repeated runs skip
    prompt processing and report impossible prompt tok/s.
    """
    tagged = f"[측정 {uuid.uuid4().hex[:8]}] {prompt}"
    payload = client.chat_payload([{"role": "user", "content": tagged}])
    payload["think"] = think
    request = client.build_request("/api/chat", payload)
    start = time.perf_counter()
    ttft: float | None = None
    final: dict[str, object] = {}
    try:
        with urlopen(request, timeout=client.config.chat_timeout * 5) as response:
            for line in response:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                message = chunk.get("message") or {}
                if ttft is None and (message.get("content") or message.get("thinking")):
                    ttft = time.perf_counter() - start
                if chunk.get("done"):
                    final = chunk
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise OllamaError(f"벤치마크 요청 실패: HTTP {exc.code} {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OllamaError("벤치마크 요청에 실패했습니다.") from exc
    total = time.perf_counter() - start
    if not final:
        raise OllamaError(
            f"스트림이 done 청크 없이 끝났습니다 ({time.perf_counter() - start:.1f}s 경과, "
            f"서버가 연결을 끊었을 수 있음)"
        )
    prompt_tokens = int(final.get("prompt_eval_count", 0))
    output_tokens = int(final.get("eval_count", 0))
    return RunStats(
        ttft=ttft if ttft is not None else total,
        total=total,
        load=int(final.get("load_duration", 0)) / NS_PER_SECOND,
        prompt_tokens=prompt_tokens,
        prompt_tps=_rate(prompt_tokens, int(final.get("prompt_eval_duration", 0))),
        output_tokens=output_tokens,
        output_tps=_rate(output_tokens, int(final.get("eval_duration", 0))),
    )


def summarize(name: str, runs: Sequence[RunStats]) -> str:
    """One markdown table row with means (TTFT also shows the median)."""
    def mean(values: Sequence[float]) -> float:
        return statistics.fmean(values) if values else 0.0

    ttfts = [r.ttft for r in runs]
    return (
        f"| {name} | {len(runs)} | {mean(ttfts):.2f} (p50 {statistics.median(ttfts):.2f}) "
        f"| {mean([r.total for r in runs]):.2f} "
        f"| {mean([r.prompt_tokens for r in runs]):.0f} | {mean([r.prompt_tps for r in runs]):.1f} "
        f"| {mean([r.output_tokens for r in runs]):.0f} | {mean([r.output_tps for r in runs]):.1f} |"
    )


def describe_offload(client: OllamaClient) -> list[str]:
    """Report how much of each loaded model sits in GPU memory (100% = no CPU offload)."""
    lines = []
    for model in client.running_models():
        size, vram = int(model.get("size", 0)), int(model.get("size_vram", 0))
        share = f"{vram / size:.0%}" if size else "?"
        lines.append(f"- {model.get('name')}: GPU {share} ({size / 1e9:.1f} GB)")
    return lines or ["- (로드된 모델 없음)"]


def bench_embedding(client: OllamaClient, texts: list[str], runs: int) -> str:
    timings = []
    for _ in range(runs):
        start = time.perf_counter()
        client.embed_query(SHORT_QUESTION)
        timings.append(time.perf_counter() - start)
    start = time.perf_counter()
    client.embed(texts)
    batch = time.perf_counter() - start
    return (
        f"- query embed: mean {statistics.fmean(timings) * 1000:.0f} ms over {runs} runs\n"
        f"- document batch embed: {len(texts)} docs in {batch * 1000:.0f} ms"
    )


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=5, help="measured runs per scenario")
    parser.add_argument("--warmup", type=int, default=1, help="discarded runs per scenario")
    parser.add_argument("--think", choices=("off", "on", "both"), default="off")
    parser.add_argument("--long-chars", type=int, default=LONG_PROMPT_CHARS)
    parser.add_argument("--skip-embedding", action="store_true")
    parser.add_argument(
        "--num-predict",
        type=int,
        default=None,
        help="output token cap (default: OLLAMA_NUM_PREDICT); thinking tokens count toward it",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # Rows appear immediately even when stdout is redirected to a file.
    sys.stdout.reconfigure(line_buffering=True)
    try:
        config = OllamaConfig.from_env()
        if args.num_predict is not None:
            config = dataclasses.replace(config, num_predict=args.num_predict)
    except (OllamaError, ValueError) as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    client = OllamaClient(config)
    prompts = {"short": SHORT_QUESTION, "long(RAG)": build_long_prompt(DATA_DIRECTORY, args.long_chars)}
    think_modes = {"off": [False], "on": [True], "both": [False, True]}[args.think]

    print(
        f"chat={config.chat_model} embed={config.embedding_model} "
        f"num_ctx={config.num_ctx} num_predict={config.num_predict}\n"
    )
    print("| scenario | n | TTFT s | total s | prompt tok | prompt tok/s | out tok | gen tok/s |")
    print("|---|---|---|---|---|---|---|---|")
    try:
        for prompt_name, prompt in prompts.items():
            for think in think_modes:
                for _ in range(args.warmup):
                    run_once(client, prompt, think)
                runs = [run_once(client, prompt, think) for _ in range(args.runs)]
                print(summarize(f"{prompt_name} think={'on' if think else 'off'}", runs), flush=True)
        print("\nGPU offload (/api/ps):")
        print("\n".join(describe_offload(client)))
        if not args.skip_embedding:
            texts = [doc.text for doc in load_all_documents(DATA_DIRECTORY)]
            print("\nEmbedding:")
            print(bench_embedding(client, texts, args.runs))
    except OllamaError as exc:
        print(f"요청 실패: {exc} ({exc.__cause__!r})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
