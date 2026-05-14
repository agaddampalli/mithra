"""Unit tests for VectorStore.

ChromaDB: uses EphemeralClient (in-memory, no server).
LiteLLM embeddings: patched via unittest.mock.patch so no network call is made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import chromadb
import pytest

from mithra.memory.vector_store import VectorStore

_EMBED_DIM = 8   # small fixed dimension for test embeddings


def _fake_post(vector: list[float] | None = None):
    """Return a mock requests.post that yields a fixed embedding."""
    v = vector or ([0.1] * _EMBED_DIM)
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"data": [{"embedding": v}]}
    return mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> VectorStore:
    # EphemeralClient shares global in-process state in chromadb 1.5.x,
    # so we use PersistentClient with a per-test tmp_path for true isolation.
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    return VectorStore(client=client)


# ---------------------------------------------------------------------------
# write()
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_upserts_into_correct_collection(self, store):
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()):
            store.write("food", "item1", "rice and beans")

        col = store._client.get_collection("food")
        result = col.get(ids=["item1"])
        assert result["ids"] == ["item1"]
        assert result["documents"] == ["rice and beans"]

    def test_write_calls_litellm_embeddings_endpoint(self, store):
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()) as mock_post:
            store.write("food", "item1", "rice")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        url = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("url", "")
        assert "4000/v1/embeddings" in url

    def test_write_metadata_contains_required_fields(self, store):
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()):
            store.write("food", "item1", "rice", metadata={"extra": "value"})

        col = store._client.get_collection("food")
        result = col.get(ids=["item1"], include=["metadatas"])
        meta = result["metadatas"][0]
        assert meta["key"] == "item1"
        assert meta["domain"] == "food"
        assert "indexed_at" in meta
        assert meta["extra"] == "value"

    def test_write_overwrites_existing_key(self, store):
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()):
            store.write("food", "item1", "original text")
            store.write("food", "item1", "updated text")

        col = store._client.get_collection("food")
        result = col.get(ids=["item1"])
        assert result["documents"] == ["updated text"]

    def test_write_creates_collection_per_domain(self, store):
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()):
            store.write("food", "a", "apple")
            store.write("money", "b", "budget")

        names = {c.name for c in store._client.list_collections()}
        assert "food" in names
        assert "money" in names


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_returns_results_with_required_fields(self, store):
        vec = [float(i) / 10 for i in range(_EMBED_DIM)]
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post(vec)):
            store.write("food", "item1", "rice")
            results = store.search("food", "rice")

        assert len(results) >= 1
        hit = results[0]
        assert "key" in hit
        assert "text" in hit
        assert "score" in hit
        assert "metadata" in hit

    def test_search_returns_correct_key_and_text(self, store):
        vec = [float(i) / 10 for i in range(_EMBED_DIM)]
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post(vec)):
            store.write("food", "rice_key", "basmati rice")
            results = store.search("food", "rice")

        assert results[0]["key"] == "rice_key"
        assert results[0]["text"] == "basmati rice"

    def test_search_returns_empty_list_for_nonexistent_collection(self, store):
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()):
            results = store.search("nonexistent_domain", "query")
        assert results == []

    def test_search_calls_litellm_embeddings_for_query(self, store):
        vec = [float(i) / 10 for i in range(_EMBED_DIM)]
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post(vec)) as mock_post:
            store.write("food", "item1", "rice")
            mock_post.reset_mock()
            store.search("food", "what food do we have")

        mock_post.assert_called_once()

    def test_search_respects_top_k(self, store):
        vec = [float(i) / 10 for i in range(_EMBED_DIM)]
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post(vec)):
            for i in range(5):
                store.write("food", f"item{i}", f"food item {i}")
            results = store.search("food", "food", top_k=2)

        assert len(results) <= 2

    def test_search_score_is_float(self, store):
        vec = [float(i) / 10 for i in range(_EMBED_DIM)]
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post(vec)):
            store.write("food", "item1", "rice")
            results = store.search("food", "rice")

        assert isinstance(results[0]["score"], float)

    def test_search_returns_empty_for_empty_collection(self, store):
        """get_or_create creates the collection but write was never called."""
        store._collection("food")   # create empty collection
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()):
            results = store.search("food", "query")
        assert results == []


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_document(self, store):
        vec = [float(i) / 10 for i in range(_EMBED_DIM)]
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post(vec)):
            store.write("food", "item1", "rice")
            store.delete("food", "item1")
            results = store.search("food", "rice")

        keys = [r["key"] for r in results]
        assert "item1" not in keys

    def test_delete_nonexistent_key_is_noop(self, store):
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()):
            store.write("food", "item1", "rice")
            # Should not raise
            store.delete("food", "ghost_key")

    def test_delete_nonexistent_collection_is_noop(self, store):
        # Should not raise even with no collection created
        store.delete("nonexistent_domain", "any_key")

    def test_delete_only_removes_targeted_key(self, store):
        vec = [float(i) / 10 for i in range(_EMBED_DIM)]
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post(vec)):
            store.write("food", "keep", "pasta")
            store.write("food", "remove", "rice")
            store.delete("food", "remove")
            results = store.search("food", "food")

        keys = [r["key"] for r in results]
        assert "keep" in keys
        assert "remove" not in keys


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_ok_when_reachable(self, store):
        h = store.health()
        assert h["status"] == "ok"
        assert h["error"] is None
        assert isinstance(h["collection_count"], int)

    def test_health_collection_count_reflects_domains(self, store):
        with patch("mithra.memory.vector_store.requests.post", return_value=_fake_post()):
            store.write("food", "a", "apple")
            store.write("money", "b", "budget")
        h = store.health()
        assert h["collection_count"] == 2

    def test_health_returns_error_when_unreachable(self):
        """Simulate ChromaDB being down by using a client whose list_collections raises."""
        broken_client = MagicMock()
        broken_client.list_collections.side_effect = ConnectionError("ChromaDB down")
        store = VectorStore(client=broken_client)
        h = store.health()
        assert h["status"] == "error"
        assert h["error"] is not None
        assert h["collection_count"] == 0
