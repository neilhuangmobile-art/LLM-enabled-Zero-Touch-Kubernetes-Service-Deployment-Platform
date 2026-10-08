"""
tests/test_rag_fusion.py
測 rag/retriever.py 的 RRF 融合邏輯（_reciprocal_rank_fusion / _fuse_candidates）。
純邏輯測試，用假造的 rank list，不需要載入任何 embedding/reranker 模型。
"""
from rag.retriever import _reciprocal_rank_fusion, _fuse_candidates


def _item(chunk_id, source="doc.md", text="text", score=0.0):
    return {"chunk_id": chunk_id, "source": source, "text": text, "score": score}


class TestReciprocalRankFusion:
    def test_single_list_rank_order_preserved(self):
        ranked = [_item("a"), _item("b"), _item("c")]
        scores = _reciprocal_rank_fusion([ranked])
        assert scores["a"] > scores["b"] > scores["c"]

    def test_item_in_both_lists_scores_higher_than_item_in_one(self):
        dense = [_item("a"), _item("b")]
        sparse = [_item("b"), _item("c")]
        scores = _reciprocal_rank_fusion([dense, sparse])
        # "b" 在兩份清單都出現（分別排第 2、第 1），"a" 只在 dense 排第 1
        assert scores["b"] > scores["a"]
        assert scores["b"] > scores["c"]

    def test_empty_lists_return_empty_scores(self):
        assert _reciprocal_rank_fusion([[], []]) == {}

    def test_top_rank_scores_higher_with_smaller_k(self):
        ranked = [_item("a"), _item("b")]
        loose = _reciprocal_rank_fusion([ranked], k=60)
        tight = _reciprocal_rank_fusion([ranked], k=1)
        # k 越小，排名差異被放大得越明顯
        assert (tight["a"] - tight["b"]) > (loose["a"] - loose["b"])


class TestFuseCandidates:
    def test_fused_results_normalized_to_0_1(self):
        dense = [_item("a"), _item("b"), _item("c")]
        sparse = [_item("b"), _item("c"), _item("a")]
        fused = _fuse_candidates(dense, sparse, pool_size=10)
        assert fused
        assert max(f["score"] for f in fused) == 1.0
        assert all(0.0 <= f["score"] <= 1.0 for f in fused)

    def test_pool_size_caps_result_count(self):
        dense = [_item(f"d{i}") for i in range(10)]
        sparse = [_item(f"d{i}") for i in range(10)]
        fused = _fuse_candidates(dense, sparse, pool_size=3)
        assert len(fused) == 3

    def test_sorted_descending_by_score(self):
        dense = [_item("a"), _item("b"), _item("c")]
        sparse = [_item("a")]
        fused = _fuse_candidates(dense, sparse, pool_size=10)
        scores = [f["score"] for f in fused]
        assert scores == sorted(scores, reverse=True)

    def test_item_only_in_dense_still_included(self):
        dense = [_item("only_dense")]
        sparse = [_item("only_sparse")]
        fused = _fuse_candidates(dense, sparse, pool_size=10)
        ids = {f["chunk_id"] for f in fused}
        assert "only_dense" in ids
        assert "only_sparse" in ids

    def test_empty_inputs_return_empty_list(self):
        assert _fuse_candidates([], [], pool_size=5) == []

    def test_preserves_source_and_text_from_original_item(self):
        dense = [_item("a", source="networking.md", text="hello")]
        fused = _fuse_candidates(dense, [], pool_size=5)
        assert fused[0]["source"] == "networking.md"
        assert fused[0]["text"] == "hello"
