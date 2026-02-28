"""Tests for src/tools/vision_tools.py.

Covers: extract_images_from_pdf, analyze_diagram, _analyze_with_google.
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
    _analyze_with_google,
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
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("AI_MODEL_PROVIDER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        result = analyze_diagram(self._dummy_bytes)

        assert result.classification == "other"
        assert result.error == "no_api_key"

    # ── Google / Gemini 2.5 Flash (highest priority) ─────────────────────────

    def test_google_key_takes_priority_over_anthropic(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-anthropic")

        expected = DiagramAnalysisResult(
            classification="accurate_stategraph",
            description="Gemini identified parallel branches.",
            has_parallel_branches=True,
            confidence=0.95,
        )

        with patch("src.tools.vision_tools._analyze_with_google", return_value=expected) as mock_g, \
             patch("src.tools.vision_tools._analyze_with_anthropic") as mock_a:
            result = analyze_diagram(self._dummy_bytes)

        mock_g.assert_called_once()
        mock_a.assert_not_called()
        assert result.classification == "accurate_stategraph"
        assert result.confidence == pytest.approx(0.95)

    def test_google_accurate_stategraph(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        expected = DiagramAnalysisResult(
            classification="accurate_stategraph",
            description="Shows parallel fan-out/fan-in StateGraph.",
            has_parallel_branches=True,
            confidence=0.90,
        )

        with patch("src.tools.vision_tools._analyze_with_google", return_value=expected):
            result = analyze_diagram(self._dummy_bytes)

        assert result.classification == "accurate_stategraph"
        assert result.has_parallel_branches is True
        assert result.confidence == pytest.approx(0.90)

    def test_google_error_returns_graceful_result(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        error_result = DiagramAnalysisResult(
            classification="other",
            description="",
            has_parallel_branches=False,
            confidence=0.0,
            error="google_error: 403 Permission denied",
        )

        with patch("src.tools.vision_tools._analyze_with_google", return_value=error_result):
            result = analyze_diagram(self._dummy_bytes)

        assert result.classification == "other"
        assert result.error is not None
        assert "google_error" in result.error

    def test_gemini_api_key_alias_accepted(self, monkeypatch):
        """GEMINI_API_KEY is an accepted alias for GOOGLE_API_KEY."""
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        expected = DiagramAnalysisResult(
            classification="generic_flowchart",
            description="Simple flowchart.",
            has_parallel_branches=False,
            confidence=0.6,
        )

        with patch("src.tools.vision_tools._analyze_with_google", return_value=expected) as mock_g:
            result = analyze_diagram(self._dummy_bytes)

        mock_g.assert_called_once()
        assert result.classification == "generic_flowchart"

    # ── Anthropic fallback ────────────────────────────────────────────────────

    def test_anthropic_accurate_stategraph(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
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

    def test_openai_fallback_when_no_google_or_anthropic_key(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
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
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
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


# ── _analyze_with_google unit tests ──────────────────────────────────────────


class TestAnalyzeWithGoogle:
    """Tests for _analyze_with_google.

    ChatGoogleGenerativeAI is lazily imported inside the function, so we patch
    'langchain_google_genai.ChatGoogleGenerativeAI' (the module attribute that
    the `from ... import` statement resolves at call time).
    """

    _dummy_bytes = b"\x89PNG fake image bytes"
    _PATCH = "langchain_google_genai.ChatGoogleGenerativeAI"

    def _make_llm_response(self, content: str) -> MagicMock:
        resp = MagicMock()
        resp.content = content
        return resp

    def test_returns_accurate_stategraph_on_valid_json(self):
        json_payload = (
            '{"classification": "accurate_stategraph", '
            '"description": "Parallel fan-out StateGraph.", '
            '"has_parallel_branches": true, "confidence": 0.88}'
        )
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = self._make_llm_response(json_payload)

        with patch(self._PATCH, return_value=mock_llm):
            result = _analyze_with_google(self._dummy_bytes, "fake-key")

        assert result.classification == "accurate_stategraph"
        assert result.has_parallel_branches is True
        assert result.confidence == pytest.approx(0.88)
        assert result.error is None

    def test_extracts_json_embedded_in_prose(self):
        """Model wraps JSON in surrounding text — regex extraction must still work."""
        prose = (
            "Sure! Here is the analysis:\n"
            '{"classification": "generic_flowchart", "description": "Simple boxes.", '
            '"has_parallel_branches": false, "confidence": 0.5}\nHope that helps!"'
        )
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = self._make_llm_response(prose)

        with patch(self._PATCH, return_value=mock_llm):
            result = _analyze_with_google(self._dummy_bytes, "fake-key")

        assert result.classification == "generic_flowchart"
        assert result.has_parallel_branches is False

    def test_uses_gemini_25_flash_model(self):
        """Verifies the model ID passed to ChatGoogleGenerativeAI."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = self._make_llm_response(
            '{"classification": "other", "description": "", '
            '"has_parallel_branches": false, "confidence": 0.0}'
        )

        with patch(self._PATCH, return_value=mock_llm) as mock_cls:
            _analyze_with_google(self._dummy_bytes, "fake-key")

        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["model"] == "gemini-2.5-flash"
        assert call_kwargs["google_api_key"] == "fake-key"

    def test_passes_image_as_base64_url(self):
        """Image bytes must be base64-encoded and sent as image_url content."""
        import sys

        img_bytes = b"\x89PNG test"
        expected_b64 = base64.standard_b64encode(img_bytes).decode()

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = self._make_llm_response(
            '{"classification": "other", "description": "", '
            '"has_parallel_branches": false, "confidence": 0.0}'
        )

        with patch(self._PATCH, return_value=mock_llm):
            _analyze_with_google(img_bytes, "fake-key")

        call_args = mock_llm.invoke.call_args
        messages = call_args.args[0]
        content = messages[0].content
        image_part = next(p for p in content if p.get("type") == "image_url")
        assert expected_b64 in image_part["image_url"]["url"]

    def test_graceful_on_import_error(self):
        """If langchain-google-genai is not installed, returns error result."""
        import sys

        with patch.dict(sys.modules, {"langchain_google_genai": None}):
            result = _analyze_with_google(self._dummy_bytes, "fake-key")

        assert result.classification == "other"
        assert result.error is not None
        assert "google_error" in result.error

    def test_graceful_on_api_exception(self):
        """Network / auth errors don't crash — returns error result."""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = RuntimeError("503 Service Unavailable")

        with patch(self._PATCH, return_value=mock_llm):
            result = _analyze_with_google(self._dummy_bytes, "fake-key")

        assert result.classification == "other"
        assert result.error is not None
        assert "google_error" in result.error

    def test_returns_parse_error_when_no_json_in_response(self):
        """Model returns plain text with no JSON — should return parse-error result."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = self._make_llm_response("This is not JSON at all.")

        with patch(self._PATCH, return_value=mock_llm):
            result = _analyze_with_google(self._dummy_bytes, "fake-key")

        assert result.classification == "other"
        assert result.description == "Parse error"
