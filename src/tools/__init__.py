# Forensic tools for RepoInvestigator, DocAnalyst and VisionInspector

from src.tools.doc_tools import (
    RUBRIC_QUERIES,
    Chunk,
    MatchedChunk,
    PdfIngestResult,
    PdfQueryResult,
    extract_file_paths_from_text,
    ingest_pdf,
    query_pdf,
)
from src.tools.repo_tools import (
    CloneResult,
    GitHistoryResult,
    GraphAnalysisResult,
    analyze_graph_structure,
    clone_repo_sandboxed,
    extract_git_history,
)
from src.tools.vision_tools import (
    DiagramAnalysisResult,
    ExtractedImage,
    analyze_diagram,
    extract_images_from_pdf,
)

__all__ = [
    # repo
    "CloneResult",
    "GitHistoryResult",
    "GraphAnalysisResult",
    "clone_repo_sandboxed",
    "extract_git_history",
    "analyze_graph_structure",
    # doc
    "Chunk",
    "MatchedChunk",
    "PdfIngestResult",
    "PdfQueryResult",
    "RUBRIC_QUERIES",
    "ingest_pdf",
    "query_pdf",
    "extract_file_paths_from_text",
    # vision
    "ExtractedImage",
    "DiagramAnalysisResult",
    "extract_images_from_pdf",
    "analyze_diagram",
]
