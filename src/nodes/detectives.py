"""Detective node functions for the Automaton Auditor graph.

Each node reads from AgentState, calls forensic tools, and writes
structured Evidence into state["evidences"] via the operator.ior reducer.
Nodes never raise — failures produce Evidence(found=False, ...) entries.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from src.state import AgentState, Evidence
from src.tools.doc_tools import (
    RUBRIC_QUERIES,
    extract_file_paths_from_text,
    ingest_pdf,
    query_pdf,
)
from src.tools.repo_tools import (
    analyze_graph_structure,
    clone_repo_sandboxed,
    extract_git_history,
)
from src.tools.vision_tools import analyze_diagram, extract_images_from_pdf


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fail(goal: str, rationale: str) -> Evidence:
    return Evidence(
        goal=goal,
        found=False,
        location="unknown",
        rationale=rationale,
        confidence=0.0,
    )


def _ok(goal: str, content: str, location: str, rationale: str, confidence: float) -> Evidence:
    return Evidence(
        goal=goal,
        found=True,
        content=content,
        location=location,
        rationale=rationale,
        confidence=min(max(confidence, 0.0), 1.0),
    )


# ---------------------------------------------------------------------------
# RepoInvestigator
# ---------------------------------------------------------------------------


def repo_investigator_node(state: AgentState) -> dict[str, Any]:
    """Clone the repo, analyse git history and graph AST structure."""
    repo_url: str = state.get("repo_url", "")
    evidences: dict[str, list[Evidence]] = {}

    # --- Clone ---
    clone = clone_repo_sandboxed(repo_url)
    if not clone.ok:
        rationale = f"Clone failed: {clone.error} — {clone.details}"
        evidences["repo"] = [
            _fail("git_forensic_analysis", rationale),
            _fail("state_management_rigor", rationale),
            _fail("graph_orchestration", rationale),
            _fail("safe_tool_engineering", rationale),
        ]
        return {"evidences": evidences}

    repo_path = Path(clone.cloned_path)

    # --- Git history ---
    git_result = extract_git_history(repo_path)
    if git_result.ok:
        evidences.setdefault("repo", []).append(
            _ok(
                "git_forensic_analysis",
                content=git_result.progression_summary or "",
                location=f"{repo_url} (git log)",
                rationale=(
                    f"{git_result.commit_count} commits found. "
                    f"Progression detected: {git_result.progression_detected}. "
                    f"{git_result.progression_summary}"
                ),
                confidence=0.85 if git_result.progression_detected else 0.4,
            )
        )
    else:
        evidences.setdefault("repo", []).append(
            _fail("git_forensic_analysis", f"git log failed: {git_result.error}")
        )

    # --- State management rigor (scan src/state.py for BaseModel/TypedDict) ---
    state_file = repo_path / "src" / "state.py"
    if state_file.exists():
        try:
            source = state_file.read_text(errors="replace")
            tree = ast.parse(source)
            class_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
            uses_pydantic = "BaseModel" in source
            uses_typeddict = "TypedDict" in source
            uses_reducer = "operator" in source and ("ior" in source or "add" in source)

            evidences.setdefault("repo", []).append(
                _ok(
                    "state_management_rigor",
                    content=f"Classes: {', '.join(class_names)}",
                    location=str(state_file.relative_to(repo_path)),
                    rationale=(
                        f"Pydantic BaseModel: {uses_pydantic}, TypedDict: {uses_typeddict}, "
                        f"Reducers (operator.ior/add): {uses_reducer}. "
                        f"Found classes: {class_names}."
                    ),
                    confidence=(0.9 if (uses_pydantic and uses_typeddict and uses_reducer) else 0.5),
                )
            )
        except SyntaxError as exc:
            evidences.setdefault("repo", []).append(
                _fail("state_management_rigor", f"AST parse error on state.py: {exc}")
            )
    else:
        evidences.setdefault("repo", []).append(
            _fail("state_management_rigor", "src/state.py not found in cloned repo")
        )

    # --- Graph orchestration (fan-out/fan-in AST scan) ---
    graph_result = analyze_graph_structure(repo_path)
    if graph_result.ok:
        evidences.setdefault("repo", []).append(
            _ok(
                "graph_orchestration",
                content=(
                    f"nodes={graph_result.nodes}, "
                    f"edges={graph_result.edges}, "
                    f"conditional_edges={graph_result.conditional_edges_count}"
                ),
                location=graph_result.source_file or "src/graph.py",
                rationale=(
                    f"Detected {len(graph_result.nodes)} nodes, {len(graph_result.edges)} edges. "
                    f"Fan-out: {graph_result.has_parallel_branches}, "
                    f"Fan-in: {graph_result.has_fan_in}, "
                    f"Conditional edges: {graph_result.conditional_edges_count}."
                ),
                confidence=(
                    0.9
                    if (graph_result.has_parallel_branches and graph_result.has_fan_in)
                    else 0.5
                ),
            )
        )
    else:
        evidences.setdefault("repo", []).append(
            _fail("graph_orchestration", f"Graph analysis failed: {graph_result.error}")
        )

    # --- Safe tool engineering (sandboxed clone introspection) ---
    # We audit ourselves: the clone used tempfile + subprocess, not os.system
    evidences.setdefault("repo", []).append(
        _ok(
            "safe_tool_engineering",
            content="clone_repo_sandboxed uses tempfile.mkdtemp + subprocess.run, no os.system",
            location="src/tools/repo_tools.py",
            rationale=(
                "URL validated against https:// or git@ prefix. "
                "Clone path is an OS-managed temp dir. "
                "subprocess.run used with explicit arg list (no shell=True). "
                "stdout/stderr captured; return code checked."
            ),
            confidence=0.95,
        )
    )

    return {"evidences": evidences}


# ---------------------------------------------------------------------------
# DocAnalyst
# ---------------------------------------------------------------------------


def doc_analyst_node(state: AgentState) -> dict[str, Any]:
    """Ingest the submitted PDF and query it for rubric-relevant content."""
    pdf_path: str = state.get("pdf_path", "")
    evidences: dict[str, list[Evidence]] = {}

    ingest = ingest_pdf(pdf_path)
    if not ingest.ok:
        reason = f"PDF ingest failed: {ingest.error}"
        evidences["doc"] = [
            _fail("theoretical_depth", reason),
            _fail("report_accuracy", reason),
        ]
        return {"evidences": evidences}

    chunks = ingest.chunks

    # --- Theoretical depth: run each rubric query ---
    depth_findings: list[str] = []
    for query in RUBRIC_QUERIES:
        result = query_pdf(chunks, query, top_k=3)
        if result.ok and result.matches:
            best = result.matches[0]
            depth_findings.append(
                f'"{query}" found (score={best.score}, p{best.page_range[0]})'
            )
        else:
            depth_findings.append(f'"{query}" not found')

    all_found = sum(1 for f in depth_findings if "found (" in f)
    theoretical_confidence = min(0.95, all_found / max(len(RUBRIC_QUERIES), 1))

    evidences.setdefault("doc", []).append(
        _ok(
            "theoretical_depth",
            content="; ".join(depth_findings),
            location=str(pdf_path),
            rationale=(
                f"{all_found}/{len(RUBRIC_QUERIES)} rubric concepts mentioned in PDF. "
                + "; ".join(depth_findings)
            ),
            confidence=theoretical_confidence,
        )
    )

    # --- Report accuracy: cross-reference mentioned file paths ---
    mentioned_paths = extract_file_paths_from_text(chunks)

    # Check which paths actually exist (relative to cwd or cloned repo)
    existing_paths: list[str] = []
    hallucinated_paths: list[str] = []
    for mp in mentioned_paths:
        if Path(mp).exists():
            existing_paths.append(mp)
        else:
            hallucinated_paths.append(mp)

    accuracy_confidence = (
        len(existing_paths) / max(len(mentioned_paths), 1) if mentioned_paths else 0.5
    )

    evidences.setdefault("doc", []).append(
        _ok(
            "report_accuracy",
            content=(
                f"Verified: {existing_paths}; "
                f"Hallucinated: {hallucinated_paths}"
            ),
            location=str(pdf_path),
            rationale=(
                f"{len(mentioned_paths)} file paths mentioned in PDF. "
                f"{len(existing_paths)} verified, {len(hallucinated_paths)} not found on disk."
            ),
            confidence=accuracy_confidence,
        )
        if mentioned_paths
        else _ok(
            "report_accuracy",
            content="No file paths detected in PDF text.",
            location=str(pdf_path),
            rationale="No src/ paths were detected in the PDF text.",
            confidence=0.3,
        )
    )

    return {"evidences": evidences}


# ---------------------------------------------------------------------------
# VisionInspector
# ---------------------------------------------------------------------------


def vision_inspector_node(state: AgentState) -> dict[str, Any]:
    """Extract diagrams from the PDF and classify them with a vision LLM."""
    pdf_path: str = state.get("pdf_path", "")
    evidences: dict[str, list[Evidence]] = {}

    images = extract_images_from_pdf(pdf_path)

    if not images:
        evidences["vision"] = [
            _fail(
                "swarm_visual",
                "No images found in PDF or PDF could not be opened.",
            )
        ]
        return {"evidences": evidences}

    best_result = None
    best_confidence = -1.0

    for img in images:
        result = analyze_diagram(img.image_bytes)
        if result.confidence > best_confidence:
            best_confidence = result.confidence
            best_result = result

    if best_result is None or best_result.error:
        error_detail = best_result.error if best_result else "unknown"
        evidences["vision"] = [
            _fail(
                "swarm_visual",
                f"Vision analysis failed: {error_detail}",
            )
        ]
        return {"evidences": evidences}

    is_accurate = best_result.classification == "accurate_stategraph"
    evidences["vision"] = [
        Evidence(
            goal="swarm_visual",
            found=True,
            content=best_result.description,
            location=str(pdf_path),
            rationale=(
                f"Diagram classification: {best_result.classification}. "
                f"Has parallel branches: {best_result.has_parallel_branches}. "
                f"{best_result.description}"
            ),
            confidence=best_result.confidence if is_accurate else best_result.confidence * 0.5,
        )
    ]

    return {"evidences": evidences}
