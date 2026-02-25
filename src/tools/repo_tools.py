"""Forensic tools for repository investigation.

Covers rubric dimensions: git_forensic_analysis, state_management_rigor,
graph_orchestration, safe_tool_engineering.
"""

import ast
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CloneResult:
    ok: bool
    cloned_path: Optional[str] = None
    error: Optional[str] = None
    details: Optional[str] = None


@dataclass
class Commit:
    hash: str
    date: str
    author: str
    message: str


@dataclass
class GitHistoryResult:
    ok: bool
    commits: list[Commit] = field(default_factory=list)
    commit_count: int = 0
    progression_detected: bool = False
    progression_summary: Optional[str] = None
    error: Optional[str] = None


@dataclass
class GraphAnalysisResult:
    ok: bool
    nodes: list[str] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    has_parallel_branches: bool = False
    has_fan_in: bool = False
    conditional_edges_count: int = 0
    source_file: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# clone_repo_sandboxed
# ---------------------------------------------------------------------------


def clone_repo_sandboxed(repo_url: str) -> CloneResult:
    """Clone a git repo into a temp directory. Never touches cwd.

    URL must start with https:// or git@; all others are rejected to
    prevent command injection.
    """
    if not (repo_url.startswith("https://") or repo_url.startswith("git@")):
        return CloneResult(
            ok=False,
            error="invalid_url",
            details=f"URL must start with https:// or git@, got: {repo_url!r}",
        )

    tmp = tempfile.mkdtemp(prefix="auditor_clone_")
    result = subprocess.run(
        ["git", "clone", "--depth", "50", repo_url, tmp],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        return CloneResult(
            ok=False,
            error="clone_failed",
            details=result.stderr.strip(),
        )
    return CloneResult(ok=True, cloned_path=tmp)


# ---------------------------------------------------------------------------
# extract_git_history
# ---------------------------------------------------------------------------

_PROGRESSION_KEYWORDS: dict[str, list[str]] = {
    "setup": ["init", "setup", "scaffold", "initial", "create", "add deps", "pyproject"],
    "tools": ["tool", "repo_tool", "doc_tool", "vision", "clone", "ingest", "ast"],
    "nodes": ["node", "detective", "investigator", "analyst", "inspector", "graph"],
    "judges": ["judge", "prosecutor", "defense", "techlead", "justice", "opinion"],
}


def extract_git_history(repo_path: str | Path) -> GitHistoryResult:
    """Extract and analyse commit history of a cloned repo."""
    path = Path(repo_path)
    if not (path / ".git").exists():
        return GitHistoryResult(ok=False, error="not_a_git_repo")

    fmt = "%H\t%ai\t%an\t%s"
    result = subprocess.run(
        ["git", "log", "--reverse", f"--pretty=format:{fmt}"],
        capture_output=True,
        text=True,
        cwd=str(path),
        timeout=30,
    )
    if result.returncode != 0:
        return GitHistoryResult(ok=False, error=f"git_log_failed: {result.stderr.strip()}")

    commits: list[Commit] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append(Commit(hash=parts[0], date=parts[1], author=parts[2], message=parts[3]))

    # Detect progression through the build phases
    phases_found: list[str] = []
    messages = " ".join(c.message.lower() for c in commits)
    for phase, keywords in _PROGRESSION_KEYWORDS.items():
        if any(kw in messages for kw in keywords):
            phases_found.append(phase)

    progression_detected = len(phases_found) >= 2
    progression_summary = (
        f"Detected phases: {', '.join(phases_found)} across {len(commits)} commits."
        if phases_found
        else "No clear build progression detected in commit messages."
    )

    return GitHistoryResult(
        ok=True,
        commits=commits,
        commit_count=len(commits),
        progression_detected=progression_detected,
        progression_summary=progression_summary,
    )


# ---------------------------------------------------------------------------
# analyze_graph_structure
# ---------------------------------------------------------------------------


class _GraphVisitor(ast.NodeVisitor):
    """AST visitor that extracts LangGraph StateGraph structure."""

    def __init__(self) -> None:
        self.has_stategraph = False
        self.nodes: list[str] = []
        self.edges: list[tuple[str, str]] = []
        self.conditional_edges_count = 0

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func

        # StateGraph(...)
        if isinstance(func, ast.Name) and func.id == "StateGraph":
            self.has_stategraph = True

        # builder.add_node("name", ...)
        if isinstance(func, ast.Attribute) and func.attr == "add_node":
            if node.args and isinstance(node.args[0], ast.Constant):
                self.nodes.append(str(node.args[0].value))

        # builder.add_edge("src", "dst")
        if isinstance(func, ast.Attribute) and func.attr == "add_edge":
            if len(node.args) >= 2:
                src = node.args[0]
                dst = node.args[1]
                if isinstance(src, ast.Constant) and isinstance(dst, ast.Constant):
                    self.edges.append((str(src.value), str(dst.value)))

        # builder.add_conditional_edges(...)
        if isinstance(func, ast.Attribute) and func.attr == "add_conditional_edges":
            self.conditional_edges_count += 1

        self.generic_visit(node)


def _find_graph_file(repo_path: Path) -> Optional[Path]:
    candidate = repo_path / "src" / "graph.py"
    if candidate.exists():
        return candidate
    for p in repo_path.rglob("*.py"):
        try:
            if "StateGraph" in p.read_text(errors="replace"):
                return p
        except OSError:
            continue
    return None


def analyze_graph_structure(repo_path: str | Path) -> GraphAnalysisResult:
    """Parse a repo's graph file with ast to detect nodes, edges, fan-out/fan-in."""
    path = Path(repo_path)
    graph_file = _find_graph_file(path)

    if graph_file is None:
        return GraphAnalysisResult(ok=False, error="file_not_found")

    try:
        source = graph_file.read_text(errors="replace")
        tree = ast.parse(source)
    except SyntaxError as exc:
        return GraphAnalysisResult(ok=False, error=f"parse_error: {exc}")

    visitor = _GraphVisitor()
    visitor.visit(tree)

    if not visitor.has_stategraph:
        return GraphAnalysisResult(
            ok=False,
            error="no_stategraph_found",
            source_file=str(graph_file),
        )

    # Detect fan-out: one source → multiple destinations
    from collections import Counter

    src_counts = Counter(src for src, _ in visitor.edges)
    dst_counts = Counter(dst for _, dst in visitor.edges)
    has_parallel = any(v >= 2 for v in src_counts.values())
    has_fan_in = any(v >= 2 for v in dst_counts.values())

    return GraphAnalysisResult(
        ok=True,
        nodes=visitor.nodes,
        edges=visitor.edges,
        has_parallel_branches=has_parallel,
        has_fan_in=has_fan_in,
        conditional_edges_count=visitor.conditional_edges_count,
        source_file=str(graph_file),
    )
