"""Tests for src/tools/doc_tools.py.

Covers: ingest_pdf, query_pdf, extract_file_paths_from_text.
PDFs are created in-memory using PyMuPDF to avoid fixture files.
"""

import os
from pathlib import Path

import pytest

from src.tools.doc_tools import (
    Chunk,
    extract_file_paths_from_text,
    ingest_pdf,
    query_pdf,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_pdf(tmp_path: Path, pages: list[str]) -> Path:
    """Create a real PDF with one text block per page using PyMuPDF."""
    import fitz  # PyMuPDF

    pdf_path = tmp_path / "test.pdf"
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


# ── ingest_pdf ───────────────────────────────────────────────────────────────


class TestIngestPdf:
    def test_returns_error_for_missing_file(self, tmp_path):
        result = ingest_pdf(tmp_path / "nope.pdf")
        assert result.ok is False
        assert result.error == "file_not_found"

    def test_single_page_pdf_produces_chunk(self, tmp_path):
        pdf = _make_pdf(tmp_path, ["Hello LangGraph world!"])
        result = ingest_pdf(pdf)

        assert result.ok is True
        assert result.page_count == 1
        assert len(result.chunks) >= 1
        combined = " ".join(c.text for c in result.chunks)
        assert "LangGraph" in combined or "Hello" in combined

    def test_multi_page_pdf(self, tmp_path):
        pages = [f"Page {i} content about LangGraph." for i in range(5)]
        pdf = _make_pdf(tmp_path, pages)
        result = ingest_pdf(pdf)

        assert result.ok is True
        assert result.page_count == 5
        assert len(result.chunks) >= 1

    def test_chunks_have_unique_ids(self, tmp_path):
        pages = ["A" * 2000, "B" * 2000, "C" * 2000]
        pdf = _make_pdf(tmp_path, pages)
        result = ingest_pdf(pdf)

        ids = [c.chunk_id for c in result.chunks]
        assert len(ids) == len(set(ids)), "Chunk IDs must be unique"

    def test_chunk_page_numbers_are_positive(self, tmp_path):
        pdf = _make_pdf(tmp_path, ["Some text on page one."])
        result = ingest_pdf(pdf)

        for chunk in result.chunks:
            assert chunk.page_start >= 1
            assert chunk.page_end >= chunk.page_start


# ── query_pdf ────────────────────────────────────────────────────────────────


class TestQueryPdf:
    def _chunks(self, texts: list[str]) -> list[Chunk]:
        return [
            Chunk(chunk_id=f"c{i}", text=t, page_start=i + 1, page_end=i + 1)
            for i, t in enumerate(texts)
        ]

    def test_returns_error_on_empty_chunks(self):
        result = query_pdf([], "fan-out")
        assert result.ok is False
        assert result.error == "no_chunks"

    def test_returns_error_on_empty_query(self):
        chunks = self._chunks(["some text"])
        result = query_pdf(chunks, "")
        assert result.ok is False
        assert result.error == "empty_query"

    def test_finds_relevant_chunk(self):
        chunks = self._chunks([
            "This document discusses Dialectical Synthesis in multi-agent systems.",
            "This page is about something completely unrelated.",
            "Fan-in and fan-out are parallel execution patterns.",
        ])
        result = query_pdf(chunks, "Dialectical Synthesis", top_k=1)

        assert result.ok is True
        assert len(result.matches) >= 1
        assert result.matches[0].chunk_id == "c0"

    def test_top_k_respected(self):
        chunks = self._chunks(
            [f"LangGraph StateGraph node {i}" for i in range(10)]
        )
        result = query_pdf(chunks, "LangGraph StateGraph", top_k=3)

        assert result.ok is True
        assert len(result.matches) <= 3

    def test_zero_score_chunks_excluded(self):
        chunks = self._chunks(["aaa bbb ccc", "xxx yyy zzz"])
        result = query_pdf(chunks, "langgraph stategraph fan-out")

        assert result.ok is True
        for m in result.matches:
            assert m.score > 0

    def test_match_has_page_range(self):
        chunks = self._chunks(["LangGraph fan-out fan-in parallel nodes"])
        result = query_pdf(chunks, "fan-out parallel")

        assert result.ok is True
        if result.matches:
            m = result.matches[0]
            assert isinstance(m.page_range, tuple)
            assert len(m.page_range) == 2


# ── extract_file_paths_from_text ─────────────────────────────────────────────


class TestExtractFilePaths:
    def _chunks(self, text: str) -> list[Chunk]:
        return [Chunk(chunk_id="c0", text=text, page_start=1, page_end=1)]

    def test_finds_python_paths(self):
        text = "See src/nodes/detectives.py and src/tools/repo_tools.py for details."
        result = extract_file_paths_from_text(self._chunks(text))

        assert "src/nodes/detectives.py" in result
        assert "src/tools/repo_tools.py" in result

    def test_finds_config_paths(self):
        text = "Configuration lives in src/graph.py and pyproject.toml is at the root."
        result = extract_file_paths_from_text(self._chunks(text))

        assert "src/graph.py" in result

    def test_deduplicates(self):
        text = "src/state.py is important. See src/state.py again."
        result = extract_file_paths_from_text(self._chunks(text))

        assert result.count("src/state.py") == 1

    def test_returns_empty_for_no_paths(self):
        text = "No file paths mentioned in this text at all."
        result = extract_file_paths_from_text(self._chunks(text))

        assert result == []

    def test_handles_multiple_chunks(self):
        chunks = [
            Chunk("c0", "See src/graph.py for the graph.", 1, 1),
            Chunk("c1", "Tools live in src/tools/doc_tools.py.", 2, 2),
        ]
        result = extract_file_paths_from_text(chunks)

        assert "src/graph.py" in result
        assert "src/tools/doc_tools.py" in result
