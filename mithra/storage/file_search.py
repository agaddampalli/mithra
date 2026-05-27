"""
FileSearchService — three-tier file retrieval.

Tier 1 (fast):  SQLite metadata — filename, mime_type, domain filters.
Tier 2 (smart): ChromaDB semantic search over extracted text chunks.
Tier 3 (full):  Load raw file bytes for downstream processing.

Health domain results always filtered to the requesting user_id only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mithra.storage.document_indexer import _collection_name

if TYPE_CHECKING:
    import chromadb
    from mithra.storage.file_adapter import FileRecord
    from mithra.storage.file_router import FileStorageRouter

logger = logging.getLogger(__name__)

_HEALTH_DOMAIN = "health"


@dataclass
class FileSearchResult:
    """A single search hit with relevance context."""

    record: "FileRecord"
    score: float = 1.0            # 1.0 for metadata hits; cosine similarity for semantic
    matched_chunks: list[str] = field(default_factory=list)
    tier: int = 1                 # 1 = metadata, 2 = semantic


class FileSearchService:
    """Searches across stored files using a tiered retrieval strategy."""

    def __init__(
        self,
        file_router: "FileStorageRouter",
        chroma_client: "chromadb.Client",
        household_id: str,
    ) -> None:
        self._router = file_router
        self._chroma = chroma_client
        self._household_id = household_id

    def search_metadata(
        self,
        domain: str,
        user_id: str,
        filename_fragment: str | None = None,
        mime_type_prefix: str | None = None,
    ) -> list[FileSearchResult]:
        """Tier 1: filter by SQLite metadata fields. Fast, exact match."""
        adapter = self._router.get_adapter(domain)
        records = adapter.list_files(self._household_id, domain, user_id)
        results: list[FileSearchResult] = []
        for r in records:
            if filename_fragment and filename_fragment.lower() not in r.filename.lower():
                continue
            if mime_type_prefix and not (r.mime_type or "").startswith(mime_type_prefix):
                continue
            results.append(FileSearchResult(record=r, score=1.0, tier=1))
        return results

    def search_semantic(
        self,
        query: str,
        domain: str,
        user_id: str,
        n_results: int = 5,
    ) -> list[FileSearchResult]:
        """Tier 2: ChromaDB semantic similarity search over indexed text chunks."""
        collection_name = _collection_name(self._household_id, domain)
        try:
            collection = self._chroma.get_collection(collection_name)
        except Exception:
            return []

        where: dict = {"user_id": user_id} if domain == _HEALTH_DOMAIN else {}
        try:
            results = collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where if where else None,
            )
        except Exception:
            logger.exception("ChromaDB query failed for domain=%s", domain)
            return []

        # Group chunks by file_id
        file_hits: dict[str, list[tuple[str, float]]] = {}
        ids_list = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        documents = results.get("documents", [[]])[0]

        for chunk_id, dist, meta, doc in zip(ids_list, distances, metadatas, documents):
            file_id = meta.get("file_id", "")
            score = 1.0 - dist  # convert distance to similarity
            file_hits.setdefault(file_id, []).append((doc, score))

        search_results: list[FileSearchResult] = []
        adapter = self._router.get_adapter(domain)
        for file_id, chunk_scores in file_hits.items():
            record = adapter.get_record(file_id, self._household_id)
            if record is None:
                continue
            # Health domain: only return results for the requesting user
            if domain == _HEALTH_DOMAIN and record.user_id != user_id:
                continue
            avg_score = sum(s for _, s in chunk_scores) / len(chunk_scores)
            matched = [c for c, _ in chunk_scores]
            search_results.append(
                FileSearchResult(record=record, score=avg_score, matched_chunks=matched, tier=2)
            )

        search_results.sort(key=lambda r: r.score, reverse=True)
        return search_results

    def get_file_content(
        self, file_id: str, domain: str, household_id: str
    ) -> bytes:
        """Tier 3: load full raw file bytes for downstream processing."""
        adapter = self._router.get_adapter(domain)
        return adapter.get_file(file_id, household_id)  # type: ignore[return-value]
