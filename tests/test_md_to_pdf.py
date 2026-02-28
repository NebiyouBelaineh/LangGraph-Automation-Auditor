"""Tests for scripts/md_to_pdf.py: mermaid extraction, process_markdown, and main().

External commands (mmdc, pandoc) are mocked; no real mermaid-cli or pandoc required.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load scripts/md_to_pdf as a module (scripts is not a package)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "md_to_pdf.py"
spec = importlib.util.spec_from_file_location("md_to_pdf", _SCRIPT_PATH)
md_to_pdf = importlib.util.module_from_spec(spec)
sys.modules["md_to_pdf"] = md_to_pdf
spec.loader.exec_module(md_to_pdf)


# ---------------------------------------------------------------------------
# Mermaid block regex
# ---------------------------------------------------------------------------


class TestMermaidBlockRegex:
    """MERMAID_BLOCK_RE extracts content between ```mermaid and ```."""

    def test_finds_single_block(self):
        content = "text\n```mermaid\nflowchart LR\n  A --> B\n```\nmore"
        matches = list(md_to_pdf.MERMAID_BLOCK_RE.finditer(content))
        assert len(matches) == 1
        assert "flowchart LR" in matches[0].group(1)
        assert "A --> B" in matches[0].group(1)

    def test_finds_multiple_blocks(self):
        content = """
```mermaid\ngraph TD\nX-->Y\n```
middle
```mermaid\nsequenceDiagram\nA->>B\n```
"""
        matches = list(md_to_pdf.MERMAID_BLOCK_RE.finditer(content))
        assert len(matches) == 2
        assert "graph TD" in matches[0].group(1)
        assert "sequenceDiagram" in matches[1].group(1)

    def test_no_mermaid_blocks(self):
        content = "just ```python\ncode\n``` and ```\nraw\n```"
        matches = list(md_to_pdf.MERMAID_BLOCK_RE.finditer(content))
        assert len(matches) == 0

    def test_mermaid_with_leading_whitespace_after_fence(self):
        content = "```mermaid  \n  A --> B\n```"
        matches = list(md_to_pdf.MERMAID_BLOCK_RE.finditer(content))
        assert len(matches) == 1
        assert "A --> B" in matches[0].group(1)


# ---------------------------------------------------------------------------
# find_mmdc
# ---------------------------------------------------------------------------


class TestFindMmdc:
    def test_returns_mmdc_when_in_path(self):
        with patch("shutil.which") as which:
            which.side_effect = lambda cmd: "/usr/bin/mmdc" if cmd == "mmdc" else None
            assert md_to_pdf.find_mmdc() == "mmdc"

    def test_returns_npx_when_mmdc_missing_but_npx_present(self):
        with patch("shutil.which") as which:
            which.side_effect = lambda cmd: "/usr/bin/npx" if cmd == "npx" else None
            assert md_to_pdf.find_mmdc() == "npx"

    def test_returns_none_when_neither_present(self):
        with patch("shutil.which", return_value=None):
            assert md_to_pdf.find_mmdc() is None


# ---------------------------------------------------------------------------
# process_markdown (with mocked render)
# ---------------------------------------------------------------------------


class TestProcessMarkdown:
    def test_replaces_mermaid_blocks_with_image_refs(self, tmp_path):
        content = "# Doc\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\nDone."
        with patch.object(
            md_to_pdf,
            "render_mermaid_to_image",
            return_value=True,
        ):
            out = md_to_pdf.process_markdown(content, tmp_path, "mmdc", "png")
        assert "```mermaid" not in out
        assert "![](" in out
        assert "diagrams/diagram_000.png" in out
        assert "# Doc" in out and "Done." in out

    def test_multiple_blocks_get_sequential_image_refs(self, tmp_path):
        content = (
            "```mermaid\ngraph A\n```\n"
            "```mermaid\ngraph B\n```\n"
        )
        with patch.object(
            md_to_pdf,
            "render_mermaid_to_image",
            return_value=True,
        ):
            out = md_to_pdf.process_markdown(content, tmp_path, "mmdc", "png")
        assert "diagrams/diagram_000.png" in out
        assert "diagrams/diagram_001.png" in out
        assert out.count("![](") == 2

    def test_uses_svg_extension_when_requested(self, tmp_path):
        content = "```mermaid\nflowchart LR\nA-->B\n```"
        with patch.object(
            md_to_pdf,
            "render_mermaid_to_image",
            return_value=True,
        ):
            out = md_to_pdf.process_markdown(content, tmp_path, "mmdc", "svg")
        assert "diagrams/diagram_000.svg" in out

    def test_leaves_block_in_place_when_render_fails(self, tmp_path):
        content = "```mermaid\ninvalid syntax\n```"
        with patch.object(
            md_to_pdf,
            "render_mermaid_to_image",
            return_value=False,
        ):
            out = md_to_pdf.process_markdown(content, tmp_path, "mmdc", "png")
        assert "```mermaid" in out
        assert "invalid syntax" in out
        assert "failed to render" in out or "mermaid block" in out


# ---------------------------------------------------------------------------
# main() — CLI and integration (mocked subprocess / deps)
# ---------------------------------------------------------------------------


class TestMain:
    def test_missing_input_returns_1(self):
        with patch("sys.argv", ["md_to_pdf", "/nonexistent/file.md", "-o", "/tmp/out.pdf"]):
            exit_code = md_to_pdf.main()
        assert exit_code == 1

    def test_skip_mermaid_calls_pandoc_with_input_and_output(self, tmp_path):
        input_md = tmp_path / "in.md"
        input_md.write_text("# Hello\n\nNo mermaid here.")
        output_pdf = tmp_path / "out.pdf"

        run_pandoc_calls = []

        def capture_pandoc(*args, **kwargs):
            run_pandoc_calls.append(args)
            return True

        with patch.object(md_to_pdf, "run_pandoc", side_effect=capture_pandoc):
            with patch("sys.argv", [
                "md_to_pdf",
                str(input_md),
                "-o", str(output_pdf),
                "--skip-mermaid",
            ]):
                exit_code = md_to_pdf.main()
        assert exit_code == 0
        assert len(run_pandoc_calls) == 1
        (pandoc_input, out_pdf, metadata_file, resource_path) = run_pandoc_calls[0]
        assert pandoc_input == input_md.resolve()
        assert out_pdf == output_pdf.resolve()

    def test_without_skip_mermaid_fails_when_mmdc_not_found(self, tmp_path):
        input_md = tmp_path / "in.md"
        input_md.write_text("# Hi\n\n```mermaid\nA-->B\n```")
        output_pdf = tmp_path / "out.pdf"

        with patch.object(md_to_pdf, "find_mmdc", return_value=None):
            with patch("sys.argv", [
                "md_to_pdf",
                str(input_md),
                "-o", str(output_pdf),
            ]):
                exit_code = md_to_pdf.main()
        assert exit_code == 1

    def test_full_flow_skip_mermaid_succeeds_when_pandoc_mocked(self, tmp_path):
        """With --skip-mermaid, main() uses input as-is and calls run_pandoc; we mock it to succeed."""
        input_md = tmp_path / "report.md"
        input_md.write_text("# Report\n\nContent.")
        output_pdf = tmp_path / "report.pdf"
        mock_pandoc = MagicMock(return_value=True)

        with patch.object(md_to_pdf, "run_pandoc", mock_pandoc):
            with patch("sys.argv", [
                "md_to_pdf",
                str(input_md),
                "-o", str(output_pdf),
                "--skip-mermaid",
            ]):
                exit_code = md_to_pdf.main()
        assert exit_code == 0
        mock_pandoc.assert_called_once()
