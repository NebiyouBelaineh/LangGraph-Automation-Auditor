"""Tests for src/tools/vision_tools.py.

Covers: extract_images_from_pdf, analyze_diagram.
LLM API calls are mocked. Real PDFs are created in-memory with PyMuPDF.
"""

import base64
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.tools.vision_tools import (
    DiagramAnalysisResult,
    ExtractedImage,
    analyze_diagram,
    extract_images_from_pdf,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_valid_png() -> bytes:
    """Build a valid 1×1 red pixel PNG programmatically."""

    def chunk(name: bytes, data: bytes) -> bytes:
        header = name + data
        return struct.pack(">I", len(data)) + header + struct.pack(">I", zlib.crc32(header) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    # Filter byte 0 (None) + RGB = 255,0,0
    raw_scanline = b"\x00\xff\x00\x00"
    idat = chunk(b"IDAT", zlib.compress(raw_scanline))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def _make_pdf_no_images(tmp_path: Path) -> Path:
    import fitz

    p = tmp_path / "no_images.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Text only, no images.", fontsize=12)
    doc.save(str(p))
    doc.close()
    return p


def _make_pdf_with_image(tmp_path: Path) -> Path:
    """Embed a valid 1×1 red pixel PNG into a PDF page."""
    import fitz

    img_path = tmp_path / "pixel.png"
    img_path.write_bytes(_make_valid_png())

    p = tmp_path / "with_image.pdf"
    doc = fitz.open()
    page = doc.new_page()
    rect = fitz.Rect(100, 100, 200, 200)
    page.insert_image(rect, filename=str(img_path))
    doc.save(str(p))
    doc.close()
    return p


# ── extract_images_from_pdf ──────────────────────────────────────────────────


class TestExtractImagesFromPdf:
    def test_returns_empty_for_missing_file(self, tmp_path):
        result = extract_images_from_pdf(tmp_path / "nonexistent.pdf")
        assert result == []

    def test_returns_empty_for_text_only_pdf(self, tmp_path):
        pdf = _make_pdf_no_images(tmp_path)
        result = extract_images_from_pdf(pdf)
        assert isinstance(result, list)
        # Text-only PDF should yield no images
        assert result == []

    def test_extracts_image_from_pdf(self, tmp_path):
        pdf = _make_pdf_with_image(tmp_path)
        result = extract_images_from_pdf(pdf)

        assert isinstance(result, list)
        assert len(result) >= 1
        img = result[0]
        assert isinstance(img, ExtractedImage)
        assert isinstance(img.image_bytes, bytes)
        assert len(img.image_bytes) > 0
        assert img.page_number == 1

    def test_extracted_image_has_index(self, tmp_path):
        pdf = _make_pdf_with_image(tmp_path)
        result = extract_images_from_pdf(pdf)

        if result:
            assert result[0].image_index >= 0


# ── analyze_diagram ───────────────────────────────────────────────────────────


class TestAnalyzeDiagram:
    _dummy_bytes = b"\x89PNG fake image bytes"

    def test_disabled_returns_other(self):
        result = analyze_diagram(self._dummy_bytes, enabled=False)

        assert result.classification == "other"
        assert result.has_parallel_branches is False
        assert result.confidence == 0.0

    def test_no_api_key_returns_graceful_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("AI_MODEL_PROVIDER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        result = analyze_diagram(self._dummy_bytes)

        assert result.classification == "other"
        assert result.error == "no_api_key"

    def test_anthropic_accurate_stategraph(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        expected = DiagramAnalysisResult(
            classification="accurate_stategraph",
            description="Shows parallel fan-out/fan-in.",
            has_parallel_branches=True,
            confidence=0.92,
        )

        with patch("src.tools.vision_tools._analyze_with_anthropic", return_value=expected):
            result = analyze_diagram(self._dummy_bytes)

        assert result.classification == "accurate_stategraph"
        assert result.has_parallel_branches is True
        assert result.confidence == pytest.approx(0.92)

    def test_openai_fallback_when_no_anthropic_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("AI_MODEL_PROVIDER_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-openai")

        expected = DiagramAnalysisResult(
            classification="linear_pipeline",
            description="A simple linear flow.",
            has_parallel_branches=False,
            confidence=0.75,
        )

        with patch("src.tools.vision_tools._analyze_with_openai", return_value=expected):
            result = analyze_diagram(self._dummy_bytes)

        assert result.classification == "linear_pipeline"
        assert result.has_parallel_branches is False

    def test_anthropic_error_returns_graceful_result(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")

        error_result = DiagramAnalysisResult(
            classification="other",
            description="",
            has_parallel_branches=False,
            confidence=0.0,
            error="anthropic_error: API error",
        )

        with patch("src.tools.vision_tools._analyze_with_anthropic", return_value=error_result):
            result = analyze_diagram(self._dummy_bytes)

        assert result.classification == "other"
        assert result.error is not None
        assert "anthropic_error" in result.error
