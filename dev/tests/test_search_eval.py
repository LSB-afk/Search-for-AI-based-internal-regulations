import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import search_eval
import server


class MetricTest(unittest.TestCase):
    def test_hit_and_recall(self):
        ranked = ["a", "b", "c"]
        self.assertEqual(search_eval.hit_at(ranked, {"a"}, 1), 1.0)
        self.assertEqual(search_eval.hit_at(ranked, {"c"}, 1), 0.0)
        self.assertEqual(search_eval.recall_at(ranked, {"a", "c"}, 3), 1.0)
        self.assertEqual(search_eval.recall_at(ranked, {"a", "z"}, 3), 0.5)

    def test_mrr(self):
        self.assertEqual(search_eval.mrr_at(["x", "a"], {"a"}, 10), 0.5)
        self.assertEqual(search_eval.mrr_at(["x", "y"], {"a"}, 10), 0.0)

    def test_ndcg_perfect_and_worst(self):
        self.assertAlmostEqual(search_eval.ndcg_at(["a", "b"], {"a", "b"}, 5), 1.0)
        self.assertEqual(search_eval.ndcg_at(["x", "y"], {"a"}, 5), 0.0)
        # 정답이 2위에 있으면 1/log2(3) / 1
        self.assertAlmostEqual(
            search_eval.ndcg_at(["x", "a"], {"a"}, 5), 1.0 / (1.4426950408889634 * 1.0986122886681098) , places=3
        )

    def test_paired_bootstrap_detects_clear_improvement(self):
        baseline = [0.0] * 20
        candidate = [1.0] * 20
        delta = search_eval.paired_bootstrap_delta(baseline, candidate, resamples=200, seed=1)
        self.assertAlmostEqual(delta["delta"], 1.0)
        self.assertGreater(delta["ci_low"], 0.9)
        self.assertEqual(delta["p_worse"], 0.0)

    def test_paired_bootstrap_is_deterministic(self):
        baseline = [0.2, 0.4, 0.6, 0.1]
        candidate = [0.3, 0.5, 0.4, 0.6]
        first = search_eval.paired_bootstrap_delta(baseline, candidate, resamples=500, seed=7)
        second = search_eval.paired_bootstrap_delta(baseline, candidate, resamples=500, seed=7)
        self.assertEqual(first, second)


class GoldSetTest(unittest.TestCase):
    def test_gold_keys_all_exist_in_sample_corpus(self):
        gold = search_eval.load_gold()
        corpus_keys = {search_eval.chunk_key(chunk) for chunk in server.sample_chunks()}
        for item in gold:
            self.assertTrue(
                item["relevant"] <= corpus_keys,
                f"unknown gold keys for query {item['query']!r}: {item['relevant'] - corpus_keys}",
            )

    def test_gold_has_reasonable_size(self):
        self.assertGreaterEqual(len(search_eval.load_gold()), 20)


class HarnessSmokeTest(unittest.TestCase):
    def test_bm25_regex_pipeline_beats_random_on_gold(self):
        import hybrid_search

        gold = search_eval.load_gold()
        chunks = server.sample_chunks()
        engine = hybrid_search.HybridSearcher(
            hybrid_search.RegexTokenizer(frozenset(server.STOPWORDS))
        )
        rows = search_eval.run_pipeline("hybrid", engine, gold, chunks)
        self.assertEqual(len(rows), len(gold))
        mean_recall5 = sum(row["recall@5"] for row in rows) / len(rows)
        # 어휘 검색만으로도 골드셋 절반 이상은 상위 5위 안에 들어야 한다.
        self.assertGreater(mean_recall5, 0.5)


if __name__ == "__main__":
    unittest.main()
