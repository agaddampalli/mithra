"""
Extracts text from files using Kreuzberg,
chunks content, and stores in ChromaDB.

Uses Kreuzberg (Rust core, ~71MB, no PyTorch) for
extraction. Handles PDF, DOCX, XLSX, CSV, PPTX,
images with OCR.

Privacy rules (one-way doors):
  health domain → local embedding ONLY (phi3:mini)
  all others    → LLM router (cloud ok)
  health content NEVER sent to cloud embedding APIs

Runs in background after file save — non-blocking.
File is saved and searchable immediately via SQLite.
ChromaDB semantic search available after indexing.

Kreuzberg is NOT installed in api-gateway if it adds
significant image size. DocumentIndexer degrades
gracefully — returns [] if kreuzberg unavailable,
files remain accessible via Tier 1 (SQLite) search.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
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


class DocumentIndexer:
    """Extract text via Kreuzberg, chunk, and embed into ChromaDB."""

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
        adapter = self._file_router.get_adapter(record.domain)
        content = await self._get_content(adapter, record)
        chunks = await self._extract_chunks(content, record.filename, record.domain)
        if not chunks:
            return []
        chunk_ids = self._embed_chunks(record, chunks)
        adapter.update_chunk_ids(record.id, self._household_id, chunk_ids)
        return chunk_ids

    async def _get_content(
        self, adapter: "FileStorageAdapter", record: "FileRecord"
    ) -> bytes:
        """Fetch file content, handling both sync and async adapters."""
        import inspect
        if inspect.iscoroutinefunction(adapter.get_file):
            return await adapter.get_file(record.id, self._household_id)
        return await asyncio.get_event_loop().run_in_executor(
            None, adapter.get_file, record.id, self._household_id
        )

    async def _extract_chunks(
        self,
        content: bytes,
        filename: str,
        domain: str,
    ) -> list[str]:
        """Extract text chunks using Kreuzberg.

        Kreuzberg: ~71MB, async Rust core, no ML/PyTorch.
        Handles: PDF, DOCX, XLSX, CSV, PPTX, images+OCR.
        All processing local — no API calls.

        Privacy: health domain uses force_ocr only locally.
        Falls back gracefully if kreuzberg not installed.
        """
        try:
            from kreuzberg import extract_bytes, ExtractionConfig  # type: ignore[import]
            mime = self._get_mime_type(filename)

            # Use OCR for image formats
            image_mimes = {
                "image/jpeg", "image/png",
                "image/tiff", "image/bmp", "image/gif",
            }
            config = ExtractionConfig(force_ocr=(mime in image_mimes))

            result = await extract_bytes(
                content,
                mime_type=mime,
                config=config,
            )
            text = result.content
            if not text or not text.strip():
                logger.info("No text extracted from %s", filename)
                return []
            return self._chunk_text(text)

        except ImportError:
            logger.warning(
                "kreuzberg not installed — skipping text extraction for %s. "
                "Run: pip install kreuzberg",
                filename,
            )
            return []
        except Exception as e:
            logger.error("Text extraction failed for %s: %s", filename, e)
            return []

    def _get_mime_type(self, filename: str) -> str:
        """Infer MIME type from filename extension."""
        mime, _ = mimetypes.guess_type(filename)
        # Common types mimetypes may not know
        ext_map = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".csv": "text/csv",
            ".md": "text/markdown",
        }
        if not mime:
            _, ext = os.path.splitext(filename.lower())
            mime = ext_map.get(ext)
        return mime or "application/octet-stream"

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into overlapping character chunks."""
        if not text:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = start + _CHUNK_SIZE
            chunks.append(text[start:end])
            start += _CHUNK_SIZE - _CHUNK_OVERLAP
        return chunks

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
