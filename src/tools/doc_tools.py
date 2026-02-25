"""Document analysis tools for the DocAnalyst detective node.

Covers rubric dimensions: theoretical_depth, report_accuracy.
"""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    chunk_id: str
    text: str
    page_start: int
    page_end: int


@dataclass
class PdfIngestResult:
    ok: bool
    chunks: list[Chunk] = field(default_factory=list)
    page_count: int = 0
    error: Optional[str] = None


@dataclass
class MatchedChunk:
    chunk_id: str
    text_snippet: str
    score: float
    page_range: tuple[int, int]


@dataclass
class PdfQueryResult:
    ok: bool
    query: str
    matches: list[MatchedChunk] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# ingest_pdf
# ---------------------------------------------------------------------------

_CHUNK_CHAR_SIZE = 1800  # ~500 tokens at ~3.6 chars/token


def ingest_pdf(path: str | Path) -> PdfIngestResult:
    """Extract text from a PDF and split into overlapping chunks."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return PdfIngestResult(ok=False, error="pymupdf_not_installed")

    pdf_path = Path(path)
    if not pdf_path.exists():
        return PdfIngestResult(ok=False, error="file_not_found")

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        return PdfIngestResult(ok=False, error=f"open_failed: {exc}")

    if doc.page_count == 0:
        return PdfIngestResult(ok=False, error="empty_pdf")

    # Collect text per page
    page_texts: list[tuple[int, str]] = []
    for page_num in range(doc.page_count):
        text = doc[page_num].get_text()
        if text.strip():
            page_texts.append((page_num + 1, text))

    if not page_texts:
        return PdfIngestResult(ok=False, error="no_text_extracted", page_count=doc.page_count)

    # Build chunks: concatenate pages and split by character window
    chunks: list[Chunk] = []
    chunk_idx = 0
    buffer = ""
    buffer_pages: list[int] = []

    for page_num, text in page_texts:
        buffer += text
        buffer_pages.append(page_num)

        while len(buffer) >= _CHUNK_CHAR_SIZE:
            slice_text = buffer[:_CHUNK_CHAR_SIZE]
            chunks.append(
                Chunk(
                    chunk_id=f"chunk_{chunk_idx:04d}",
                    text=slice_text,
                    page_start=buffer_pages[0],
                    page_end=buffer_pages[-1],
                )
            )
            chunk_idx += 1
            # Slide forward by 75% of chunk size to give overlap
            advance = int(_CHUNK_CHAR_SIZE * 0.75)
            buffer = buffer[advance:]
            buffer_pages = [page_num]

    if buffer.strip():
        chunks.append(
            Chunk(
                chunk_id=f"chunk_{chunk_idx:04d}",
                text=buffer,
                page_start=buffer_pages[0] if buffer_pages else page_texts[-1][0],
                page_end=page_texts[-1][0],
            )
        )

    return PdfIngestResult(ok=True, chunks=chunks, page_count=doc.page_count)


# ---------------------------------------------------------------------------
# query_pdf  — keyword / TF scoring (no external vector store)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\b[a-zA-Z][a-zA-Z0-9\-_]*\b", text.lower())


def _tf_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not doc_tokens:
        return 0.0
    doc_len = len(doc_tokens)
    score = sum(doc_tokens.count(qt) / doc_len for qt in query_tokens)
    # log-scale to avoid huge docs dominating
    return score * math.log(1 + doc_len)


# Pre-defined rubric queries; callers may pass arbitrary queries too
RUBRIC_QUERIES = [
    "Dialectical Synthesis",
    "Fan-In Fan-Out",
    "Metacognition",
    "State Synchronization",
    "LangGraph StateGraph",
    "Pydantic BaseModel TypedDict",
]


def query_pdf(
    chunks: list[Chunk],
    query: str,
    top_k: int = 5,
) -> PdfQueryResult:
    """Rank chunks by TF score for `query`; return top_k matches."""
    if not chunks:
        return PdfQueryResult(ok=False, query=query, error="no_chunks")

    query_tokens = _tokenize(query)
    if not query_tokens:
        return PdfQueryResult(ok=False, query=query, error="empty_query")

    scored: list[tuple[float, Chunk]] = []
    for chunk in chunks:
        doc_tokens = _tokenize(chunk.text)
        score = _tf_score(query_tokens, doc_tokens)
        scored.append((score, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    matches = [
        MatchedChunk(
            chunk_id=c.chunk_id,
            text_snippet=c.text[:300].replace("\n", " "),
            score=round(s, 4),
            page_range=(c.page_start, c.page_end),
        )
        for s, c in top
        if s > 0
    ]

    return PdfQueryResult(ok=True, query=query, matches=matches)


# ---------------------------------------------------------------------------
# extract_file_paths_from_text
# ---------------------------------------------------------------------------

# Matches paths like: src/nodes/detectives.py, src/tools/__init__.py
_FILE_PATH_RE = re.compile(
    r"\b(?:src|tests?|docs?|reports?|scripts?)/[\w/\.\-]+\.(?:py|json|toml|md|txt)\b"
)


def extract_file_paths_from_text(chunks: list[Chunk]) -> list[str]:
    """Scan chunks for file path mentions; return deduplicated list."""
    found: set[str] = set()
    for chunk in chunks:
        for match in _FILE_PATH_RE.finditer(chunk.text):
            found.add(match.group(0))
    return sorted(found)
