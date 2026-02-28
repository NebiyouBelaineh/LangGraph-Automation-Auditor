"""Forensic tools for repository investigation.

Covers rubric dimensions: git_forensic_analysis, state_management_rigor,
graph_orchestration, safe_tool_engineering.

Each function exists in two forms:
- The raw function (e.g. clone_repo_sandboxed) — deterministic, returns a dataclass.
- The @tool wrapper (e.g. clone_repo) — LangChain tool for use in LLM agent loops.

Security design
---------------
- All git operations run inside ``tempfile.TemporaryDirectory()``.
- ``subprocess.run()`` is always called with an explicit argument list (no ``shell=True``),
  eliminating shell-injection via URL metacharacters.
- URL allow-listing: only ``https://`` and ``git@`` prefixes are accepted.
- URL sanitization: whitespace, newlines, and null bytes are stripped/rejected before
  the URL reaches subprocess, preventing embedded-command injection even without ``shell=True``.
- Authentication failures are classified into specific error codes with user-facing messages.
- Cloned directories are tracked in ``_ACTIVE_CLONES``; callers must invoke
  ``cleanup_clone_dir()`` after analysis.  ``TemporaryDirectory.cleanup()`` is also
  called automatically when the process exits via Python's finaliser chain.
"""

import ast
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Module-level clone registry
# ---------------------------------------------------------------------------
# Keeps TemporaryDirectory objects alive so the directory persists across
# multiple tool calls.  Callers MUST call cleanup_clone_dir() when done.
_ACTIVE_CLONES: dict[str, "tempfile.TemporaryDirectory[str]"] = {}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CloneResult:
    ok: bool
    cloned_path: Optional[str] = None
    error: Optional[str] = None
    details: Optional[str] = None
    user_message: Optional[str] = None  # human-readable explanation for the caller


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

# Known stderr patterns → (error_code, user-facing message)
_AUTH_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    (
        "authentication failed",
        "auth_failed",
        "Git authentication failed. Check your credentials (HTTPS token or SSH key).",
    ),
    (
        "could not read username",
        "auth_failed",
        "Git authentication failed: no username supplied. Use an HTTPS token URL or configure SSH.",
    ),
    (
        "permission denied",
        "auth_failed",
        "Permission denied by the remote. Verify your SSH key or HTTPS token has read access.",
    ),
    (
        "repository not found",
        "repo_not_found",
        "Repository not found. Check the URL and ensure the repo is public (or credentials are correct).",
    ),
    (
        "could not resolve host",
        "network_error",
        "DNS resolution failed. Check network connectivity and the hostname in the URL.",
    ),
    (
        "connection refused",
        "network_error",
        "Connection refused by the remote host. Check network connectivity.",
    ),
    (
        "timed out",
        "timeout_error",
        "Git clone timed out. The repository may be too large or the network is slow.",
    ),
    (
        "ssl certificate",
        "ssl_error",
        "SSL certificate verification failed. Check system CA certificates.",
    ),
    (
        "rate limit",
        "rate_limited",
        "GitHub rate limit reached. Wait before retrying or authenticate to raise the limit.",
    ),
]


def _classify_clone_error(stderr: str) -> tuple[str, str]:
    """Map raw git stderr to (error_code, user_message).

    Returns ("clone_failed", <raw stderr>) as the fallback.
    """
    lower = stderr.lower()
    for pattern, code, message in _AUTH_ERROR_PATTERNS:
        if pattern in lower:
            return code, message
    return "clone_failed", f"Git clone failed: {stderr.strip()}"


def _sanitize_url(url: str) -> tuple[str, str | None]:
    """Sanitize a URL before validation and subprocess use.

    Returns (sanitized_url, error_message_or_None).

    Control characters (newlines, carriage returns, null bytes, tabs) are
    rejected on the raw URL before stripping, because they can be used for
    header injection or argument smuggling even without ``shell=True``.
    Leading/trailing ASCII spaces are then stripped.
    """
    forbidden = {"\n", "\r", "\x00", "\t"}
    if any(ch in url for ch in forbidden):
        return url, f"URL contains illegal control characters: {url!r}"
    return url.strip(), None


def clone_repo_sandboxed(
    repo_url: str,
    *,
    branch: Optional[str] = None,
    depth: int = 50,
) -> CloneResult:
    """Clone a git repo into a ``TemporaryDirectory``. Never touches cwd.

    Security layers applied (in order):
    1. URL sanitization — strip whitespace; reject newlines and null bytes.
    2. URL allow-listing — only ``https://`` and ``git@`` prefixes accepted.
    3. Temp-directory isolation — clone target is always inside the OS temp path,
       never the auditor's working directory.
    4. No ``shell=True`` — arguments are passed as a list; the shell never
       interprets URL metacharacters.
    5. Return-code checking — non-zero exit is classified into a specific error
       code (auth_failed, repo_not_found, network_error, …) with a user-facing
       message.

    The created ``TemporaryDirectory`` is kept alive in ``_ACTIVE_CLONES``.
    Callers MUST call ``cleanup_clone_dir(cloned_path)`` after analysis.

    Args:
        repo_url: Remote URL to clone.
        branch:   Branch, tag, or commit SHA passed as ``--branch`` to git clone.
                  ``None`` clones the remote HEAD (default).
        depth:    Shallow-clone depth passed as ``--depth`` to git clone.
                  ``0`` performs a full (non-shallow) clone.
    """
    # ── 1. URL sanitization ─────────────────────────────────────────────────
    repo_url, sanitize_err = _sanitize_url(repo_url)
    if sanitize_err:
        return CloneResult(
            ok=False,
            error="invalid_url",
            details=sanitize_err,
            user_message="The provided URL contains illegal characters and was rejected.",
        )

    # ── 2. URL allow-listing ────────────────────────────────────────────────
    if not (repo_url.startswith("https://") or repo_url.startswith("git@")):
        return CloneResult(
            ok=False,
            error="invalid_url",
            details=f"URL must start with https:// or git@, got: {repo_url!r}",
            user_message=(
                "Invalid URL scheme. Only HTTPS (https://) and SSH (git@) URLs are accepted."
            ),
        )

    # ── 3. Temp-directory isolation ─────────────────────────────────────────
    # TemporaryDirectory() is used (not mkdtemp) so that the OS-level cleanup
    # guarantee is maintained.  The object is stored in _ACTIVE_CLONES to keep
    # the directory alive until the caller explicitly calls cleanup_clone_dir().
    td = tempfile.TemporaryDirectory(prefix="auditor_clone_")
    _ACTIVE_CLONES[td.name] = td
    tmp = td.name

    # ── 4. Subprocess with explicit argument list (no shell=True) ───────────
    cmd: list[str] = ["git", "clone"]
    if depth > 0:
        cmd += ["--depth", str(depth)]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo_url, tmp]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # ── 5. Return-code checking with classified error messages ──────────────
    if result.returncode != 0:
        # Directory is empty; remove it immediately so we don't leave orphans.
        _ACTIVE_CLONES.pop(tmp, None)
        td.cleanup()
        error_code, user_message = _classify_clone_error(result.stderr)
        return CloneResult(
            ok=False,
            error=error_code,
            details=result.stderr.strip(),
            user_message=user_message,
        )

    return CloneResult(ok=True, cloned_path=tmp)


def cleanup_clone_dir(cloned_path: str) -> bool:
    """Remove the TemporaryDirectory created by ``clone_repo_sandboxed``.

    Returns True if the directory was found and cleaned up, False otherwise.
    Always safe to call; silently handles missing paths.
    """
    td = _ACTIVE_CLONES.pop(cloned_path, None)
    if td is not None:
        td.cleanup()
        return True
    # Fallback for paths not in the registry (e.g. tests using real temp dirs).
    path = Path(cloned_path)
    if path.exists() and path.is_dir():
        shutil.rmtree(cloned_path, ignore_errors=True)
        return True
    return False


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


# ---------------------------------------------------------------------------
# LangChain tool wrappers — used by LLM detective agents
# ---------------------------------------------------------------------------

from langchain_core.tools import tool  # noqa: E402


@tool
def clone_repo(repo_url: str) -> dict:
    """Clone a GitHub repository to a temporary directory for forensic analysis.

    Returns cloned_path which must be passed to subsequent repo analysis tools.
    Call cleanup_clone when analysis is complete to remove the temporary directory.
    """
    result = clone_repo_sandboxed(repo_url)
    return {
        "ok": result.ok,
        "cloned_path": result.cloned_path or "",
        "error": result.error or "",
        "details": result.details or "",
        "user_message": result.user_message or "",
    }


@tool
def cleanup_clone(cloned_path: str) -> dict:
    """Remove the temporary directory created by clone_repo.

    Must be called after analysis is complete to avoid orphaned temp directories.
    Returns whether the directory was found and successfully removed.
    """
    removed = cleanup_clone_dir(cloned_path)
    return {"ok": removed, "cloned_path": cloned_path}


@tool
def read_git_history(cloned_path: str) -> dict:
    """Read and analyze the git commit history of a cloned repository.

    Returns commit count, build-phase progression detection, and recent commit messages.
    """
    result = extract_git_history(cloned_path)
    if not result.ok:
        return {"ok": False, "error": result.error}
    commits_summary = [
        {"hash": c.hash[:8], "date": c.date, "message": c.message}
        for c in result.commits[:30]
    ]
    return {
        "ok": True,
        "commit_count": result.commit_count,
        "progression_detected": result.progression_detected,
        "progression_summary": result.progression_summary,
        "commits": commits_summary,
    }


@tool
def run_graph_structure_analysis(cloned_path: str) -> dict:
    """Analyze the LangGraph StateGraph structure in a cloned repository using AST parsing.

    Returns nodes, edges, fan-out/fan-in detection, and conditional edge count.
    """
    result = analyze_graph_structure(cloned_path)
    if not result.ok:
        return {"ok": False, "error": result.error, "source_file": result.source_file}
    return {
        "ok": True,
        "nodes": result.nodes,
        "edges": result.edges,
        "has_parallel_branches": result.has_parallel_branches,
        "has_fan_in": result.has_fan_in,
        "conditional_edges_count": result.conditional_edges_count,
        "source_file": result.source_file,
    }


@tool
def scan_file_for_patterns(cloned_path: str, relative_path: str, patterns: list[str]) -> dict:
    """Check whether specific text patterns exist in a file within the cloned repository.

    Use to verify presence of code constructs, class names, imports, or keywords.
    relative_path is relative to the repo root (e.g. 'src/state.py').
    """
    file_path = Path(cloned_path) / relative_path
    if not file_path.exists():
        return {"ok": False, "exists": False, "error": f"File not found: {relative_path}"}
    try:
        source = file_path.read_text(errors="replace")
        patterns_found = {p: p in source for p in patterns}
        return {"ok": True, "exists": True, "patterns_found": patterns_found}
    except OSError as exc:
        return {"ok": False, "exists": True, "error": str(exc)}


@tool
def list_repo_files(cloned_path: str, glob_pattern: str = "**/*.py") -> dict:
    """List files in the cloned repository matching a glob pattern.

    Use to discover whether specific files or directories exist in the repo.
    Returns up to 60 matching file paths relative to the repo root.
    """
    path = Path(cloned_path)
    if not path.exists():
        return {"ok": False, "error": f"Path not found: {cloned_path}"}
    try:
        matched = [str(p.relative_to(path)) for p in path.glob(glob_pattern) if p.is_file()]
        return {"ok": True, "files": matched[:60], "total_matched": len(matched)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
