"""Hybrid lexical/dense retrieval for the regulation search server.

구성 요소는 모두 선택적으로 동작한다.

- 형태소 분석: kiwipiepy가 있으면 Kiwi, 없으면 정규식 토크나이저로 폴백.
- BM25: 순수 파이썬 구현이라 항상 사용 가능.
- 임베딩/리랭커: fastembed 또는 sentence-transformers가 설치되고 모델이
  로컬 캐시에 있을 때만 활성화되며, 실패 시 검색을 중단하지 않고
  파이프라인 상태에 오류 종류만 기록한다(폐쇄망 원칙).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import threading
import unicodedata
from collections import Counter
from typing import Any, Callable, Iterable, Sequence

DEFAULT_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_RRF_K = 60
DEFAULT_DENSE_MIN_SIM = 0.30
DEFAULT_RERANK_TOP = 50
_TOKEN_CACHE_LIMIT = 50_000


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tokenizers
# ---------------------------------------------------------------------------

class RegexTokenizer:
    """서버의 기존 규칙과 동일한 정규식 + 조사 제거 토크나이저."""

    name = "regex"

    def __init__(self, stopwords: frozenset[str] | set[str] = frozenset()) -> None:
        self._stopwords = set(stopwords)

    def tokenize(self, text: str) -> list[str]:
        text = unicodedata.normalize("NFC", text)
        raw = re.findall(r"[가-힣A-Za-z0-9]{2,}", text.lower())
        tokens: list[str] = []
        for token in raw:
            if token in self._stopwords:
                continue
            if re.search(r"[가-힣]", token) and len(token) > 2:
                token = re.sub(
                    r"(으로써|으로|에게|에서|부터|까지|에는|에게는|은|는|이|가|을|를|의|와|과|도|만|로)$",
                    "",
                    token,
                )
            if len(token) >= 2 and token not in self._stopwords:
                tokens.append(token)
        return tokens


class KiwiTokenizer:
    """Kiwi 형태소 분석으로 내용어(명사/어근/용언/외래어/숫자)만 추출한다."""

    name = "kiwi"
    _CONTENT_TAGS = {"NNG", "NNP", "SL", "SH", "XR", "VV", "VA"}
    _NUMBER_TAGS = {"SN"}

    def __init__(self, stopwords: frozenset[str] | set[str] = frozenset()) -> None:
        from kiwipiepy import Kiwi  # noqa: PLC0415 - optional dependency

        self._kiwi = Kiwi()
        self._stopwords = set(stopwords)
        self._lock = threading.Lock()

    def tokenize(self, text: str) -> list[str]:
        text = unicodedata.normalize("NFC", text)
        if not text.strip():
            return []
        with self._lock:
            morphs = self._kiwi.tokenize(text)
        tokens: list[str] = []
        for morph in morphs:
            tag = morph.tag
            form = morph.form.lower()
            if tag in self._NUMBER_TAGS:
                tokens.append(form)
                continue
            if tag not in self._CONTENT_TAGS:
                continue
            if form in self._stopwords:
                continue
            if tag in {"VV", "VA"} and len(form) < 2:
                continue
            if not form:
                continue
            tokens.append(form)
        return tokens


def build_tokenizer(
    preferred: str = "auto",
    stopwords: frozenset[str] | set[str] = frozenset(),
) -> Any:
    """kiwi를 우선 시도하고 불가하면 regex로 폴백한다."""
    if preferred in ("auto", "kiwi"):
        try:
            return KiwiTokenizer(stopwords)
        except Exception:
            if preferred == "kiwi":
                raise
    return RegexTokenizer(stopwords)


# ---------------------------------------------------------------------------
# BM25 (Okapi)
# ---------------------------------------------------------------------------

class BM25Index:
    def __init__(
        self,
        documents: Sequence[Sequence[str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b = b
        self._doc_counts = [Counter(doc) for doc in documents]
        self._doc_lens = [sum(counts.values()) for counts in self._doc_counts]
        self._avgdl = (sum(self._doc_lens) / len(self._doc_lens)) if self._doc_lens else 0.0
        total = len(documents)
        df: Counter[str] = Counter()
        for counts in self._doc_counts:
            df.update(counts.keys())
        self._idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def score(self, query_tokens: Sequence[str]) -> list[float]:
        scores = [0.0] * len(self._doc_counts)
        if not query_tokens:
            return scores
        unique_terms = dict.fromkeys(query_tokens)
        for idx, counts in enumerate(self._doc_counts):
            doc_len = self._doc_lens[idx]
            if not doc_len:
                continue
            norm = self.k1 * (1 - self.b + self.b * doc_len / self._avgdl) if self._avgdl else self.k1
            score = 0.0
            for term in unique_terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                score += self._idf.get(term, 0.0) * (tf * (self.k1 + 1)) / (tf + norm)
            scores[idx] = score
        return scores

    def matched_terms(self, doc_index: int, query_tokens: Sequence[str]) -> list[str]:
        counts = self._doc_counts[doc_index]
        return sorted({term for term in query_tokens if counts.get(term)})


# ---------------------------------------------------------------------------
# Dense encoders / rerankers (선택적 백엔드)
# ---------------------------------------------------------------------------

class FastEmbedEncoder:
    backend = "fastembed"

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding  # noqa: PLC0415 - optional dependency

        self.model_name = model_name
        self._model = TextEmbedding(model_name)

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [_normalize(vector) for vector in self._model.embed(list(texts))]

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [_normalize(vector) for vector in self._model.embed(list(texts))]


class SentenceTransformerEncoder:
    backend = "sentence-transformers"

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self.model_name = model_name
        self._model = SentenceTransformer(model_name, device="cpu")
        self._is_e5 = "e5" in model_name.lower()

    def _encode(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        payload = [f"{prefix}{text}" for text in texts] if self._is_e5 else list(texts)
        vectors = self._model.encode(payload, normalize_embeddings=True)
        return [list(map(float, vector)) for vector in vectors]

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, "query: ")

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, "passage: ")


class FastEmbedReranker:
    backend = "fastembed"

    def __init__(self, model_name: str) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: PLC0415

        self.model_name = model_name
        self._model = TextCrossEncoder(model_name)

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        return [float(value) for value in self._model.rerank(query, list(documents))]


class SentenceTransformerReranker:
    backend = "sentence-transformers"

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder  # noqa: PLC0415

        self.model_name = model_name
        self._model = CrossEncoder(model_name, device="cpu")

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        pairs = [(query, document) for document in documents]
        return [float(value) for value in self._model.predict(pairs)]


def _normalize(vector: Iterable[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        return values
    return [value / norm for value in values]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right)))


def rrf_fuse(
    rank_lists: dict[str, Sequence[int]],
    weights: dict[str, float],
    *,
    k: int = DEFAULT_RRF_K,
) -> dict[int, float]:
    """Reciprocal Rank Fusion: score(d) = Σ weight / (k + rank)."""
    fused: dict[int, float] = {}
    for engine, ordered_indices in rank_lists.items():
        weight = weights.get(engine, 1.0)
        for rank, doc_index in enumerate(ordered_indices, start=1):
            fused[doc_index] = fused.get(doc_index, 0.0) + weight / (k + rank)
    return fused


# ---------------------------------------------------------------------------
# Hybrid searcher
# ---------------------------------------------------------------------------

class HybridSearcher:
    """BM25 + (선택) 임베딩 + (선택) 리랭커를 RRF로 결합한다."""

    def __init__(
        self,
        tokenizer: Any,
        *,
        encoder_factory: Callable[[], Any] | None = None,
        reranker_factory: Callable[[], Any] | None = None,
        rrf_k: int = DEFAULT_RRF_K,
        bm25_weight: float = 1.0,
        dense_weight: float = 1.0,
        dense_min_sim: float = DEFAULT_DENSE_MIN_SIM,
        rerank_top: int = DEFAULT_RERANK_TOP,
    ) -> None:
        self.tokenizer = tokenizer
        self.rrf_k = rrf_k
        self.bm25_weight = bm25_weight
        self.dense_weight = dense_weight
        self.dense_min_sim = dense_min_sim
        self.rerank_top = rerank_top
        self._encoder_factory = encoder_factory
        self._reranker_factory = reranker_factory
        self._encoder: Any = None
        self._reranker: Any = None
        self._dense_status = "disabled" if encoder_factory is None else "not_loaded"
        self._rerank_status = "disabled" if reranker_factory is None else "not_loaded"
        self._dense_error: str | None = None
        self._rerank_error: str | None = None
        self._lock = threading.Lock()
        self._token_cache: dict[str, list[str]] = {}
        self._vector_cache: dict[str, list[float]] = {}

    # -- component lifecycle -------------------------------------------------

    def _get_encoder(self) -> Any:
        if self._encoder_factory is None or self._dense_status == "error":
            return None
        with self._lock:
            if self._encoder is None and self._dense_status == "not_loaded":
                try:
                    self._encoder = self._encoder_factory()
                    self._dense_status = "ready"
                except Exception as exc:
                    self._dense_status = "error"
                    self._dense_error = type(exc).__name__
            return self._encoder

    def _get_reranker(self) -> Any:
        if self._reranker_factory is None or self._rerank_status == "error":
            return None
        with self._lock:
            if self._reranker is None and self._rerank_status == "not_loaded":
                try:
                    self._reranker = self._reranker_factory()
                    self._rerank_status = "ready"
                except Exception as exc:
                    self._rerank_status = "error"
                    self._rerank_error = type(exc).__name__
            return self._reranker

    def pipeline(self) -> dict[str, Any]:
        return {
            "tokenizer": self.tokenizer.name,
            "bm25": True,
            "dense": {
                "status": self._dense_status,
                "backend": getattr(self._encoder, "backend", None),
                "model": getattr(self._encoder, "model_name", None),
                "error": self._dense_error,
            },
            "reranker": {
                "status": self._rerank_status,
                "backend": getattr(self._reranker, "backend", None),
                "model": getattr(self._reranker, "model_name", None),
                "error": self._rerank_error,
            },
            "fusion": f"rrf(k={self.rrf_k})",
        }

    # -- caches --------------------------------------------------------------

    def _doc_tokens(self, text: str) -> list[str]:
        key = _sha1(f"{self.tokenizer.name}\x00{text}")
        cached = self._token_cache.get(key)
        if cached is None:
            cached = self.tokenizer.tokenize(text)
            if len(self._token_cache) >= _TOKEN_CACHE_LIMIT:
                self._token_cache.clear()
            self._token_cache[key] = cached
        return cached

    def _passage_vectors(self, encoder: Any, texts: Sequence[str]) -> list[list[float]] | None:
        keys = [_sha1(f"{encoder.model_name}\x00{text}") for text in texts]
        missing = [(idx, text) for idx, (key, text) in enumerate(zip(keys, texts)) if key not in self._vector_cache]
        if missing:
            try:
                vectors = encoder.encode_passages([text for _, text in missing])
            except Exception as exc:
                self._dense_status = "error"
                self._dense_error = type(exc).__name__
                return None
            for (idx, _), vector in zip(missing, vectors):
                self._vector_cache[keys[idx]] = vector
        return [self._vector_cache[key] for key in keys]

    # -- ranking -------------------------------------------------------------

    def rank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        query_tokens: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """문서 목록에 대한 하이브리드 순위를 계산한다.

        반환 항목: index, score, matched_terms, scores{bm25,dense,rerank,fused}.
        BM25와 dense 어느 쪽에도 매칭되지 않은 문서는 제외한다.
        """
        if not documents:
            return []
        if query_tokens is None:
            query_tokens = self.tokenizer.tokenize(query)

        doc_tokens = [self._doc_tokens(text) for text in documents]
        bm25 = BM25Index(doc_tokens)
        bm25_scores = bm25.score(query_tokens)
        bm25_ranked = sorted(
            (idx for idx, value in enumerate(bm25_scores) if value > 0),
            key=lambda idx: bm25_scores[idx],
            reverse=True,
        )

        dense_scores: list[float] | None = None
        dense_ranked: list[int] = []
        encoder = self._get_encoder()
        if encoder is not None:
            passage_vectors = self._passage_vectors(encoder, documents)
            if passage_vectors is not None:
                try:
                    query_vector = encoder.encode_queries([query])[0]
                except Exception as exc:
                    self._dense_status = "error"
                    self._dense_error = type(exc).__name__
                else:
                    dense_scores = [cosine(query_vector, vector) for vector in passage_vectors]
                    dense_ranked = sorted(
                        (idx for idx, value in enumerate(dense_scores) if value >= self.dense_min_sim),
                        key=lambda idx: dense_scores[idx],
                        reverse=True,
                    )

        rank_lists: dict[str, Sequence[int]] = {"bm25": bm25_ranked}
        weights = {"bm25": self.bm25_weight}
        if dense_scores is not None:
            rank_lists["dense"] = dense_ranked
            weights["dense"] = self.dense_weight
        fused = rrf_fuse(rank_lists, weights, k=self.rrf_k)
        if not fused:
            return []

        ordered = sorted(fused, key=lambda idx: fused[idx], reverse=True)
        rerank_scores: dict[int, float] = {}
        reranker = self._get_reranker()
        if reranker is not None and ordered:
            head = ordered[: self.rerank_top]
            try:
                head_scores = reranker.score(query, [documents[idx] for idx in head])
            except Exception as exc:
                self._rerank_status = "error"
                self._rerank_error = type(exc).__name__
            else:
                rerank_scores = dict(zip(head, head_scores))
                tail = ordered[self.rerank_top:]
                head = sorted(head, key=lambda idx: rerank_scores[idx], reverse=True)
                ordered = head + tail

        results: list[dict[str, Any]] = []
        max_fused = max(fused.values())
        head_count = len(rerank_scores)
        if rerank_scores:
            low = min(rerank_scores.values())
            high = max(rerank_scores.values())
            spread = (high - low) or 1.0
        for position, idx in enumerate(ordered):
            # 최종 점수는 항상 양수이고 순위와 단조 감소하도록 정규화한다.
            if rerank_scores and position < head_count:
                normalized = 0.5 + 0.5 * ((rerank_scores[idx] - low) / spread)
            else:
                normalized = 0.45 * (fused[idx] / max_fused)
            results.append(
                {
                    "index": idx,
                    "score": round(100 * normalized, 4),
                    "matched_terms": bm25.matched_terms(idx, query_tokens),
                    "scores": {
                        "bm25": round(bm25_scores[idx], 4),
                        "dense": round(dense_scores[idx], 4) if dense_scores is not None else None,
                        "rerank": round(rerank_scores[idx], 4) if idx in rerank_scores else None,
                        "fused": round(fused[idx], 6),
                    },
                }
            )
        return results


# ---------------------------------------------------------------------------
# Environment-driven construction
# ---------------------------------------------------------------------------

def _encoder_factory_from_env(env: dict[str, str]) -> Callable[[], Any] | None:
    # 폐쇄망 원칙: 임베딩 백엔드는 모델 파일이 준비된 환경에서만 명시적으로 켠다.
    if env.get("REG_RAG_DENSE", "0") != "1":
        return None
    model_name = env.get("REG_RAG_EMBED_MODEL", DEFAULT_EMBED_MODEL)
    backend = env.get("REG_RAG_EMBED_BACKEND", "auto")

    def factory() -> Any:
        errors: list[str] = []
        if backend in ("auto", "fastembed"):
            try:
                return FastEmbedEncoder(model_name)
            except Exception as exc:
                errors.append(f"fastembed: {type(exc).__name__}")
                if backend == "fastembed":
                    raise
        if backend in ("auto", "sentence-transformers"):
            try:
                return SentenceTransformerEncoder(model_name)
            except Exception as exc:
                errors.append(f"sentence-transformers: {type(exc).__name__}")
                if backend == "sentence-transformers":
                    raise
        raise RuntimeError("; ".join(errors) or "no dense backend available")

    return factory


def _reranker_factory_from_env(env: dict[str, str]) -> Callable[[], Any] | None:
    model_name = env.get("REG_RAG_RERANK_MODEL", "").strip()
    if not model_name:
        return None
    backend = env.get("REG_RAG_RERANK_BACKEND", "auto")

    def factory() -> Any:
        errors: list[str] = []
        if backend in ("auto", "fastembed"):
            try:
                return FastEmbedReranker(model_name)
            except Exception as exc:
                errors.append(f"fastembed: {type(exc).__name__}")
                if backend == "fastembed":
                    raise
        if backend in ("auto", "sentence-transformers"):
            try:
                return SentenceTransformerReranker(model_name)
            except Exception as exc:
                errors.append(f"sentence-transformers: {type(exc).__name__}")
                if backend == "sentence-transformers":
                    raise
        raise RuntimeError("; ".join(errors) or "no reranker backend available")

    return factory


def build_from_env(
    env: dict[str, str] | None = None,
    *,
    stopwords: frozenset[str] | set[str] = frozenset(),
) -> HybridSearcher:
    env = dict(os.environ) if env is None else env
    tokenizer = build_tokenizer(env.get("REG_RAG_TOKENIZER", "auto"), stopwords)
    return HybridSearcher(
        tokenizer,
        encoder_factory=_encoder_factory_from_env(env),
        reranker_factory=_reranker_factory_from_env(env),
        rrf_k=int(env.get("REG_RAG_RRF_K", str(DEFAULT_RRF_K))),
        bm25_weight=float(env.get("REG_RAG_BM25_WEIGHT", "1.0")),
        dense_weight=float(env.get("REG_RAG_DENSE_WEIGHT", "1.0")),
        dense_min_sim=float(env.get("REG_RAG_DENSE_MIN_SIM", str(DEFAULT_DENSE_MIN_SIM))),
        rerank_top=int(env.get("REG_RAG_RERANK_TOP", str(DEFAULT_RERANK_TOP))),
    )


_ENGINE: HybridSearcher | None = None
_ENGINE_LOCK = threading.Lock()


def get_engine(stopwords: frozenset[str] | set[str] = frozenset()) -> HybridSearcher:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = build_from_env(stopwords=stopwords)
        return _ENGINE


def reset_engine() -> None:
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = None
