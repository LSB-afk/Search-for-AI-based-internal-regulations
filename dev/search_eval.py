#!/usr/bin/env python3
"""검색 품질(매칭율) 실증 평가 하네스.

골드셋(`eval/gold_queries.jsonl`)의 질의별 정답 조항과 실제 검색 순위를
비교하여 Hit@1, Recall@k, MRR, nDCG를 계산하고, 파이프라인 조합별
(기존 TF-IDF / BM25+정규식 / BM25+Kiwi / +임베딩 / +리랭커) 성능 차이를
paired bootstrap 신뢰구간과 함께 보고한다.

사용 예:
    python search_eval.py                 # 사용 가능한 모든 파이프라인 비교
    python search_eval.py --no-dense      # 임베딩 없이 어휘 파이프라인만
    python search_eval.py --json out.json # 결과를 JSON으로 저장
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from pathlib import Path
from typing import Any, Callable

import hybrid_search
import server

ROOT = Path(__file__).resolve().parent
GOLD_FILE = ROOT / "eval" / "gold_queries.jsonl"
METRIC_NAMES = ("hit@1", "recall@3", "recall@5", "mrr@10", "ndcg@5")
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 42


def chunk_key(chunk: dict[str, Any]) -> str:
    return f"{chunk.get('doc_title')}|{chunk.get('section_title')}|{chunk.get('effective_from')}"


def load_gold(path: Path = GOLD_FILE) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        items.append(
            {
                "query": raw["query"],
                "relevant": set(raw["relevant"]),
                "as_of": raw.get("as_of"),
                "role": raw.get("role", "admin"),
                "category": raw.get("category", "unspecified"),
            }
        )
    return items


# ---------------------------------------------------------------------------
# Ranking metrics
# ---------------------------------------------------------------------------

def hit_at(ranked: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if any(key in relevant for key in ranked[:k]) else 0.0


def recall_at(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(relevant.intersection(ranked[:k])) / len(relevant)


def mrr_at(ranked: list[str], relevant: set[str], k: int) -> float:
    for position, key in enumerate(ranked[:k], start=1):
        if key in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, key in enumerate(ranked[:k], start=1)
        if key in relevant
    )
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def query_metrics(ranked: list[str], relevant: set[str]) -> dict[str, float]:
    return {
        "hit@1": hit_at(ranked, relevant, 1),
        "recall@3": recall_at(ranked, relevant, 3),
        "recall@5": recall_at(ranked, relevant, 5),
        "mrr@10": mrr_at(ranked, relevant, 10),
        "ndcg@5": ndcg_at(ranked, relevant, 5),
    }


def paired_bootstrap_delta(
    baseline: list[float],
    candidate: list[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float]:
    """질의 단위 paired bootstrap으로 평균 차이의 95% CI를 추정한다."""
    if len(baseline) != len(candidate) or not baseline:
        raise ValueError("baseline and candidate must be equal-length, non-empty lists")
    deltas = [c - b for b, c in zip(baseline, candidate)]
    rng = random.Random(seed)
    count = len(deltas)
    means = []
    for _ in range(resamples):
        sample = [deltas[rng.randrange(count)] for _ in range(count)]
        means.append(sum(sample) / count)
    means.sort()
    lower = means[int(0.025 * resamples)]
    upper = means[min(int(0.975 * resamples), resamples - 1)]
    worse_or_equal = sum(1 for mean in means if mean <= 0) / resamples
    return {
        "delta": sum(deltas) / count,
        "ci_low": lower,
        "ci_high": upper,
        "p_worse": worse_or_equal,
    }


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------

def run_pipeline(
    mode: str,
    engine: hybrid_search.HybridSearcher | None,
    gold: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    previous_mode = os.environ.get("REG_RAG_SEARCH_ENGINE")
    os.environ["REG_RAG_SEARCH_ENGINE"] = mode
    with hybrid_search._ENGINE_LOCK:
        previous_engine = hybrid_search._ENGINE
        hybrid_search._ENGINE = engine
    rows: list[dict[str, Any]] = []
    try:
        for item in gold:
            payload = server.search_chunks(
                chunks,
                item["query"],
                item["role"],
                item["as_of"],
                limit=10,
            )
            ranked = [chunk_key(result) for result in payload["results"]]
            rows.append(
                {
                    "query": item["query"],
                    "category": item["category"],
                    "ranked": ranked,
                    **query_metrics(ranked, item["relevant"]),
                }
            )
    finally:
        if previous_mode is None:
            os.environ.pop("REG_RAG_SEARCH_ENGINE", None)
        else:
            os.environ["REG_RAG_SEARCH_ENGINE"] = previous_mode
        with hybrid_search._ENGINE_LOCK:
            hybrid_search._ENGINE = previous_engine
    return rows


def build_pipelines(include_dense: bool, include_rerank: bool) -> list[dict[str, Any]]:
    """사용 가능한 파이프라인 조합을 구성한다. 불가한 조합은 사유와 함께 스킵."""
    stopwords = frozenset(server.STOPWORDS)
    pipelines: list[dict[str, Any]] = [
        {"name": "legacy(TF-IDF)", "mode": "legacy", "engine": None},
        {
            "name": "bm25+regex",
            "mode": "hybrid",
            "engine": hybrid_search.HybridSearcher(hybrid_search.RegexTokenizer(stopwords)),
        },
    ]
    try:
        kiwi_tokenizer = hybrid_search.KiwiTokenizer(stopwords)
        pipelines.append(
            {
                "name": "bm25+kiwi",
                "mode": "hybrid",
                "engine": hybrid_search.HybridSearcher(kiwi_tokenizer),
            }
        )
    except Exception as exc:
        kiwi_tokenizer = None
        pipelines.append({"name": "bm25+kiwi", "skip": f"kiwipiepy unavailable: {type(exc).__name__}"})

    lexical_tokenizer = kiwi_tokenizer or hybrid_search.RegexTokenizer(stopwords)
    if include_dense:
        dense_env = dict(os.environ)
        dense_env["REG_RAG_DENSE"] = "1"
        encoder_factory = hybrid_search._encoder_factory_from_env(dense_env)
        encoder = None
        if encoder_factory is not None:
            try:
                encoder = encoder_factory()
            except Exception as exc:
                pipelines.append({"name": "hybrid+dense", "skip": f"dense unavailable: {type(exc).__name__}"})
        if encoder is not None:
            pipelines.append(
                {
                    "name": "hybrid+dense",
                    "mode": "hybrid",
                    "engine": hybrid_search.HybridSearcher(
                        lexical_tokenizer, encoder_factory=lambda: encoder
                    ),
                }
            )
            if include_rerank:
                reranker_factory = hybrid_search._reranker_factory_from_env(dict(os.environ))
                if reranker_factory is None:
                    pipelines.append({"name": "hybrid+dense+rerank", "skip": "REG_RAG_RERANK_MODEL not set"})
                else:
                    try:
                        reranker = reranker_factory()
                    except Exception as exc:
                        pipelines.append(
                            {"name": "hybrid+dense+rerank", "skip": f"reranker unavailable: {type(exc).__name__}"}
                        )
                    else:
                        pipelines.append(
                            {
                                "name": "hybrid+dense+rerank",
                                "mode": "hybrid",
                                "engine": hybrid_search.HybridSearcher(
                                    lexical_tokenizer,
                                    encoder_factory=lambda: encoder,
                                    reranker_factory=lambda: reranker,
                                ),
                            }
                        )
    return pipelines


def main() -> int:
    parser = argparse.ArgumentParser(description="Regulation search matching-rate evaluation")
    parser.add_argument("--gold", default=str(GOLD_FILE), help="gold query JSONL path")
    parser.add_argument("--json", default=None, help="write full per-query results to this JSON file")
    parser.add_argument("--no-dense", action="store_true", help="skip embedding pipelines")
    parser.add_argument("--no-rerank", action="store_true", help="skip reranker pipeline")
    args = parser.parse_args()

    gold = load_gold(Path(args.gold))
    chunks = server.sample_chunks()
    corpus_keys = {chunk_key(chunk) for chunk in chunks}
    for item in gold:
        unknown = item["relevant"] - corpus_keys
        if unknown:
            print(f"gold error: unknown keys {unknown} for query {item['query']!r}", file=sys.stderr)
            return 2

    pipelines = build_pipelines(not args.no_dense, not args.no_rerank)
    report: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] | None = None
    for pipeline in pipelines:
        if "skip" in pipeline:
            report.append({"pipeline": pipeline["name"], "skipped": pipeline["skip"]})
            continue
        rows = run_pipeline(pipeline["mode"], pipeline.get("engine"), gold, chunks)
        summary = {
            metric: sum(row[metric] for row in rows) / len(rows) for metric in METRIC_NAMES
        }
        entry: dict[str, Any] = {"pipeline": pipeline["name"], "summary": summary, "rows": rows}
        if baseline_rows is None:
            baseline_rows = rows
        else:
            entry["ndcg5_vs_baseline"] = paired_bootstrap_delta(
                [row["ndcg@5"] for row in baseline_rows],
                [row["ndcg@5"] for row in rows],
            )
        report.append(entry)

    print(f"\n질의 {len(gold)}건 · 코퍼스 {len(chunks)}청크 · baseline = {report[0]['pipeline']}\n")
    header = "| 파이프라인 | Hit@1 | Recall@3 | Recall@5 | MRR@10 | nDCG@5 | ΔnDCG@5 [95% CI] |"
    print(header)
    print("|" + "---|" * 7)
    for entry in report:
        if "skipped" in entry:
            print(f"| {entry['pipeline']} | - | - | - | - | - | skipped: {entry['skipped']} |")
            continue
        summary = entry["summary"]
        if "ndcg5_vs_baseline" in entry:
            delta = entry["ndcg5_vs_baseline"]
            delta_text = f"{delta['delta']:+.3f} [{delta['ci_low']:+.3f}, {delta['ci_high']:+.3f}]"
        else:
            delta_text = "baseline"
        print(
            f"| {entry['pipeline']} "
            f"| {summary['hit@1']:.3f} | {summary['recall@3']:.3f} | {summary['recall@5']:.3f} "
            f"| {summary['mrr@10']:.3f} | {summary['ndcg@5']:.3f} | {delta_text} |"
        )

    if args.json:
        serializable = json.loads(json.dumps(report, default=list, ensure_ascii=False))
        Path(args.json).write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nsaved: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
