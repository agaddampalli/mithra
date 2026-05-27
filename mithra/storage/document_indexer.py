"""
DocumentIndexer — Docling extraction + ChromaDB embedding for uploaded files.

Runs async and non-blocking. Updates FileRecord.chunk_ids after indexing.
Health domain uses a separate ChromaDB collection and never calls cloud LLMs.

Supported formats: PDF, DOCX, PPTX, XLSX, images (via Docling).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import chromadb
    from mithra.storage.file_adapter import FileRecord, FileStorageAdapter
    from mithra.storage.file_router import FileStorageRouter

logger = logging.getLogger(__name__)

_HEALTH_DOMAIN = "health"
_CHUNK_SIZE = 400       # characters per chunk
_CHUNK_OVERLAP = 80


def _collection_name(household_id: str, domain: str) -> str:
    """Separate collection per domain. Health gets its own isolated collection."""
    if domain == _HEALTH_DOMAIN:
        return f"health_{household_id}"
    return f"docs_{household_id}"


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping character chunks."""
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


class DocumentIndexer:
    """Extract text via Docling, embed into ChromaDB. Runs in a thread pool."""

    def __init__(
        self,
        chroma_client: "chromadb.Client",
        file_router: "FileStorageRouter",
        household_id: str,
    ) -> None:
        self._chroma = chroma_client
        self._file_router = file_router
        self._household_id = household_id

    async def index_file(self, record: "FileRecord") -> list[str]:
        """
        Extract and index a file asynchronously.

        Returns a list of ChromaDB chunk IDs. Updates the FileRecord's chunk_ids
        in the backing store.
        """
        return await asyncio.get_event_loop().run_in_executor(
            None, self._index_sync, record
        )

    def _index_sync(self, record: "FileRecord") -> list[str]:
        """Blocking extraction and embedding. Called from thread pool."""
        try:
            adapter = self._file_router.get_adapter(record.domain)
            content = self._get_content_sync(adapter, record)
            text = self._extract_text(content, record.filename, record.mime_type)
            if not text:
                logger.warning("No text extracted from %s", record.id)
                return []
            chunks = _chunk_text(text)
            chunk_ids = self._embed_chunks(record, chunks)
            adapter.update_chunk_ids(record.id, self._household_id, chunk_ids)
            return chunk_ids
        except Exception:
            logger.exception("Failed to index file %s", record.id)
            return []

    def _get_content_sync(
        self, adapter: "FileStorageAdapter", record: "FileRecord"
    ) -> bytes:
        """Fetch file content synchronously (LocalFileAdapter path)."""
        # LocalFileAdapter.get_file is synchronous
        if hasattr(adapter.get_file, "__func__") or not asyncio.iscoroutinefunction(adapter.get_file):
            return adapter.get_file(record.id, self._household_id)  # type: ignore[return-value]
        # Fallback: run in new event loop (should not happen in practice)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(adapter.get_file(record.id, self._household_id))
        finally:
            loop.close()

    def _extract_text(
        self, content: bytes, filename: str, mime_type: str | None
    ) -> str:
        """Use Docling to extract text from document content."""
        try:
            from docling.document_converter import DocumentConverter  # type: ignore[import]
        except ImportError:
            logger.warning("docling not installed — skipping extraction")
            return ""

        suffix = Path(filename).suffix.lower() or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            converter = DocumentConverter()
            result = converter.convert(tmp_path)
            return result.document.export_to_markdown()
        except Exception:
            logger.exception("Docling failed for %s", filename)
            return ""
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _embed_chunks(self, record: "FileRecord", chunks: list[str]) -> list[str]:
        """Add text chunks to ChromaDB. Returns assigned chunk IDs."""
        collection_name = _collection_name(self._household_id, record.domain)
        collection = self._chroma.get_or_create_collection(collection_name)
        chunk_ids = [f"{record.id}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "file_id": record.id,
                "domain": record.domain,
                "user_id": record.user_id,
                "filename": record.filename,
                "chunk_index": i,
            }
            for i in range(len(chunks))
        ]
        collection.add(documents=chunks, ids=chunk_ids, metadatas=metadatas)
        return chunk_ids
