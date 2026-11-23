"""ai·워커 로그의 metric= 줄을 README 표(Measured Limits, 검증 결과)로 묶는다. 표준 라이브러리만 쓴다.

    kubectl logs deploy/cloud-ai --since=24h > ai.log
    kubectl logs deploy/cloud-backend-worker --since=24h > worker.log
    cat ai.log worker.log | python3 scripts/metrics.py

중앙값(p50)과 p90 을 쓴다. 표본 수(n)를 함께 찍으니 n 이 작으면 추정으로 읽는다.
prompt_tok_s 는 KV 캐시가 재사용되면 비현실적으로 커지므로(README 10,269 tok/s 사례) prompt_tokens >= 100 인 요청만 쓴다.
"""

import re
import statistics
import sys
from collections import Counter

PAIR = re.compile(r"(\w+)=(\S+)")
MIN_PROMPT_TOKENS = 100


def parse(lines):
    rows = {"generate": [], "embed": [], "job": []}
    for line in lines:
        fields = dict(PAIR.findall(line))
        if fields.get("metric") in rows:
            rows[fields["metric"]].append(fields)
    return rows


def nums(rows, key, keep=lambda r: True):
    out = []
    for r in rows:
        try:
            if keep(r):
                out.append(float(r[key]))
        except (KeyError, ValueError):
            pass
    return out


def stat(values, fmt="{:.2f}"):
    if not values:
        return "-", "-", 0
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
    return fmt.format(statistics.median(values)), fmt.format(p90), len(values)


def report(rows) -> str:
    gen, emb, jobs = rows["generate"], rows["embed"], rows["job"]

    def real_prompt(r):
        return r.get("prompt_tokens", "None").isdigit() and int(r["prompt_tokens"]) >= MIN_PROMPT_TOKENS

    table = [
        ("Generation (tok/s)", nums(gen, "output_tok_s"), "{:.2f}"),
        ("Prompt processing (tok/s)", nums(gen, "prompt_tok_s", real_prompt), "{:.1f}"),
        ("TTFT (s)", nums(gen, "ttft_s"), "{:.1f}"),
        ("Generate total (s)", nums(gen, "total_s"), "{:.1f}"),
        ("Query embedding (s)", nums(emb, "wall_s", lambda r: r.get("inputs") == "1"), "{:.2f}"),
        ("Job end-to-end incl. queue (s)", nums(jobs, "e2e_s"), "{:.1f}"),
    ]
    out = ["| Metric | p50 | p90 | n |", "| :-- | --: | --: | --: |"]
    for name, values, fmt in table:
        p50, p90, n = stat(values, fmt)
        out.append(f"| {name} | {p50} | {p90} | {n} |")

    truncated = sum(r.get("done_reason") == "length" for r in gen)
    out += ["", f"truncated (num_predict hit): {truncated}/{len(gen)}"]

    if jobs:
        status = Counter(r.get("status") for r in jobs)
        source = Counter(r.get("source") for r in jobs if r.get("status") == "done")
        reasons = Counter(x for r in jobs for x in r.get("reason", "-").split(",") if x != "-")
        n = len(jobs)
        out += ["", "| Job result | count | share |", "| :-- | --: | --: |"]
        out += [f"| {k} | {v} | {v / n:.0%} |" for k, v in status.most_common()]
        out += ["", "done source: " + ", ".join(f"{k}={v}" for k, v in source.most_common())]
        out += ["reasons: " + (", ".join(f"{k}={v}" for k, v in reasons.most_common()) or "-")]
    return "\n".join(out)


if __name__ == "__main__":
    print(report(parse(sys.stdin)))
