import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hybrid_search
from hybrid_search import (
    BM25Index,
    HybridSearcher,
    RegexTokenizer,
    build_tokenizer,
    cosine,
    rrf_fuse,
)


class FakeEncoder:
    backend = "fake"
    model_name = "fake-encoder"

    def __init__(self, query_vector, passage_vectors):
        self._query_vector = query_vector
        self._passage_vectors = passage_vectors

    def encode_queries(self, texts):
        return [self._query_vector for _ in texts]

    def encode_passages(self, texts):
        return self._passage_vectors[: len(texts)]


class FakeReranker:
    backend = "fake"
    model_name = "fake-reranker"

    def __init__(self, scores):
        self._scores = scores

    def score(self, query, documents):
        return self._scores[: len(documents)]


class RegexTokenizerTest(unittest.TestCase):
    def test_strips_josa_and_stopwords(self):
        tokenizer = RegexTokenizer({"그리고"})
        tokens = tokenizer.tokenize("그리고 징계양정을 감사부서에서")
        self.assertIn("징계양정", tokens)
        self.assertIn("감사부서", tokens)
        self.assertNotIn("그리고", tokens)


class BuildTokenizerTest(unittest.TestCase):
    def test_auto_falls_back_to_regex_when_kiwi_missing(self):
        original = hybrid_search.KiwiTokenizer
        hybrid_search.KiwiTokenizer = None  # force TypeError on construction
        try:
            tokenizer = build_tokenizer("auto")
        finally:
            hybrid_search.KiwiTokenizer = original
        self.assertEqual(tokenizer.name, "regex")

    def test_explicit_kiwi_raises_when_unavailable(self):
        original = hybrid_search.KiwiTokenizer
        hybrid_search.KiwiTokenizer = None
        try:
            with self.assertRaises(TypeError):
                build_tokenizer("kiwi")
        finally:
            hybrid_search.KiwiTokenizer = original


class BM25IndexTest(unittest.TestCase):
    def test_higher_term_frequency_scores_higher(self):
        index = BM25Index([["징계", "징계", "기준"], ["징계", "휴가"], ["회계", "지출"]])
        scores = index.score(["징계"])
        self.assertGreater(scores[0], scores[1])
        self.assertEqual(scores[2], 0.0)

    def test_rare_terms_outweigh_common_terms(self):
        index = BM25Index([["징계", "감사"], ["징계", "회계"], ["징계", "복무"]])
        scores = index.score(["감사"])
        self.assertGreater(scores[0], scores[1])

    def test_matched_terms_reports_intersection_only(self):
        index = BM25Index([["징계", "기준"]])
        self.assertEqual(index.matched_terms(0, ["징계", "휴가"]), ["징계"])

    def test_empty_corpus_and_query(self):
        self.assertEqual(BM25Index([]).score(["징계"]), [])
        self.assertEqual(BM25Index([["징계"]]).score([]), [0.0])


class RrfFuseTest(unittest.TestCase):
    def test_document_ranked_by_both_engines_wins(self):
        fused = rrf_fuse({"bm25": [0, 1], "dense": [0, 2]}, {"bm25": 1.0, "dense": 1.0}, k=60)
        self.assertGreater(fused[0], fused[1])
        self.assertGreater(fused[0], fused[2])

    def test_weights_change_ordering(self):
        fused = rrf_fuse({"bm25": [0], "dense": [1]}, {"bm25": 0.1, "dense": 2.0}, k=60)
        self.assertGreater(fused[1], fused[0])


class HybridSearcherTest(unittest.TestCase):
    DOCS = [
        "인사규정 별표1 징계양정 기준 금품수수 가중",
        "회계규정 계약과 지출 예산 증빙",
        "재난안전 지침 우선 적용",
    ]

    def test_bm25_only_ranking_and_matched_terms(self):
        searcher = HybridSearcher(RegexTokenizer())
        ranked = searcher.rank("징계양정 기준", self.DOCS)
        self.assertEqual(ranked[0]["index"], 0)
        self.assertIn("징계양정", ranked[0]["matched_terms"])
        self.assertEqual({item["index"] for item in ranked}, {0})

    def test_unmatched_documents_are_dropped(self):
        searcher = HybridSearcher(RegexTokenizer())
        self.assertEqual(searcher.rank("존재하지않는말", self.DOCS), [])

    def test_dense_rescues_lexically_unmatched_document(self):
        encoder = FakeEncoder([1.0, 0.0], [[0.1, 0.9], [0.95, 0.05], [0.0, 1.0]])
        searcher = HybridSearcher(
            RegexTokenizer(), encoder_factory=lambda: encoder, dense_min_sim=0.5
        )
        ranked = searcher.rank("전혀다른표현", self.DOCS)
        self.assertEqual([item["index"] for item in ranked], [1])
        self.assertIsNotNone(ranked[0]["scores"]["dense"])
        self.assertEqual(ranked[0]["matched_terms"], [])

    def test_dense_failure_degrades_to_bm25(self):
        def broken_factory():
            raise RuntimeError("model missing")

        searcher = HybridSearcher(RegexTokenizer(), encoder_factory=broken_factory)
        ranked = searcher.rank("징계양정", self.DOCS)
        self.assertEqual(ranked[0]["index"], 0)
        self.assertEqual(searcher.pipeline()["dense"]["status"], "error")
        self.assertEqual(searcher.pipeline()["dense"]["error"], "RuntimeError")

    def test_reranker_reorders_head(self):
        encoder = FakeEncoder([1.0, 0.0], [[0.9, 0.1], [0.8, 0.2], [0.7, 0.3]])
        reranker = FakeReranker([0.1, 0.9, 0.5])
        searcher = HybridSearcher(
            RegexTokenizer(),
            encoder_factory=lambda: encoder,
            reranker_factory=lambda: reranker,
            dense_min_sim=0.1,
        )
        ranked = searcher.rank("규정", self.DOCS)
        self.assertEqual(len(ranked), 3)
        rerank_scores = [item["scores"]["rerank"] for item in ranked]
        self.assertEqual(rerank_scores, sorted(rerank_scores, reverse=True))
        final_scores = [item["score"] for item in ranked]
        self.assertEqual(final_scores, sorted(final_scores, reverse=True))
        self.assertTrue(all(score > 0 for score in final_scores))

    def test_scores_are_positive_and_monotonic_without_reranker(self):
        searcher = HybridSearcher(RegexTokenizer())
        ranked = searcher.rank("규정 지침", self.DOCS)
        final_scores = [item["score"] for item in ranked]
        self.assertEqual(final_scores, sorted(final_scores, reverse=True))
        self.assertTrue(all(score > 0 for score in final_scores))

    def test_pipeline_reports_component_status(self):
        searcher = HybridSearcher(RegexTokenizer())
        pipeline = searcher.pipeline()
        self.assertEqual(pipeline["tokenizer"], "regex")
        self.assertTrue(pipeline["bm25"])
        self.assertEqual(pipeline["dense"]["status"], "disabled")
        self.assertEqual(pipeline["reranker"]["status"], "disabled")


class KiwiTokenizerIfAvailableTest(unittest.TestCase):
    def test_kiwi_extracts_lemma_from_inflected_form(self):
        try:
            tokenizer = hybrid_search.KiwiTokenizer()
        except Exception:
            self.skipTest("kiwipiepy not installed")
        tokens = tokenizer.tokenize("징계했을 때 감봉되는 기준")
        self.assertIn("징계", tokens)
        self.assertIn("감봉", tokens)
        self.assertIn("기준", tokens)


if __name__ == "__main__":
    unittest.main()
