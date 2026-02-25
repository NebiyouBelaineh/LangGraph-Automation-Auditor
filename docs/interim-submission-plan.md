# Interim Submission: Task Breakdown and Plan

**Target:** Interim submission (Wednesday 21:00 UTC)  
**Source:** FDE Challenge Week 2 — The Automaton Auditor

---

## Task 1: Production Environment (State + Infra)

**Scope:** Typed state, package manager, env vars, observability.

### Deliverables


| Deliverable          | Requirement                                                                                                                                                     |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**src/state.py`**   | Pydantic/TypedDict definitions: `Evidence`, `JudicialOpinion`, `AgentState` with reducers `operator.add` (for `opinions`) and `operator.ior` (for `evidences`). |
| `**pyproject.toml`** | Dependencies managed with **uv**; lockfile present.                                                                                                             |
| `**.env.example`**   | List of required env vars (e.g. `OPENAI_API_KEY`, `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY` if using LangSmith). No real secrets.                             |


### Plan

1. **State first**
  Copy or adapt the doc's `Evidence`, `JudicialOpinion`, and `AgentState` (with `Annotated[... , operator.ior]` for `evidences`, `Annotated[... , operator.add]` for `opinions`) so parallel nodes don't overwrite each other.
2. **Project setup**
  Init with `uv init`, add `langgraph`, `langchain-*`, `pydantic`, `python-dotenv`, etc. Use `uv lock` and document in README.
3. **Env template**
  Create `.env.example` with placeholder keys and a short comment on what each is for; add "copy to `.env` and fill" in README.
4. **Observability (optional for interim but good)**
  Set `LANGCHAIN_TRACING_V2=true` and use LangSmith so you can debug the detective graph later.

**Done when:** You can import `AgentState`, `Evidence`, `JudicialOpinion` from `src.state` and run `uv sync` successfully; `.env.example` is committed.

---

## Task 2: Repo Tools (RepoInvestigator Backend)

**Scope:** Sandboxed clone, git history, AST-based graph analysis.

### Deliverables


| Deliverable                   | Requirement                                                                                                                                                                                    |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**src/tools/repo_tools.py`** | Sandboxed `git clone` (e.g. inside `tempfile.TemporaryDirectory()`), `git log` extraction, AST-based graph structure analysis. No cloning into cwd; no raw `os.system` with unsanitized input. |


### Plan

1. **Clone in a sandbox**
  Use `tempfile.TemporaryDirectory()`; run `git clone <url>` via `subprocess.run()` with a path inside that directory; handle return codes and stderr; validate/limit `repo_url` (e.g. allow only expected URL shapes).
2. **Git history**
  Implement something like `extract_git_history(path: str)` that runs `git log --oneline --reverse` (and optionally with timestamps) and returns structured data (list of commits/messages) for the rubric's "Git Forensic Analysis".
3. **Graph structure (AST)**
  Implement `analyze_graph_structure(path: str)` that: finds `src/graph.py` (or equivalent), uses Python's `ast` to find `StateGraph` usage, `add_edge` / `add_conditional_edges` calls, and reports whether the graph has parallel branches and a synchronization point (fan-out/fan-in). Prefer AST over regex.
4. **Error handling**
  Catch clone failures, missing dirs, and parse errors; return clear errors or structured "not found" results instead of crashing.

**Done when:** Given a repo path, you can clone (in temp dir), get git log, and get an AST-derived description of the graph structure; no secrets in code, no `os.system` with user input.

---

## Task 3: Doc Tools (DocAnalyst Backend)

**Scope:** PDF ingestion and chunked querying (RAG-lite).

### Deliverables


| Deliverable                  | Requirement                                                                                                                                                                                          |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**src/tools/doc_tools.py`** | `ingest_pdf(path: str)` (or equivalent) that chunks the PDF and supports queries (e.g. "What does the report say about Dialectical Synthesis?"). RAG-lite: don't dump the whole PDF into one prompt. |


### Plan

1. **PDF → text**
  Use a library (e.g. Docling, PyMuPDF, or pdfplumber) to extract text; handle multi-page and basic layout.
2. **Chunking**
  Split text into chunks (e.g. by section or by token limit) and store them in a simple in-memory structure (list of chunks with optional metadata).
3. **Query interface**
  Expose a function that takes a query string and returns relevant chunks (e.g. simple keyword search, or embed chunks + query and return top-k). The goal is to answer rubric questions like "Dialectical Synthesis", "Metacognition", "Fan-In / Fan-Out" without sending the full PDF.
4. **Optional: cross-reference**
  If time allows, add a small helper that extracts file paths from text (e.g. `src/...`) for later cross-check with RepoInvestigator; not strictly required for interim.

**Done when:** Given a PDF path, you can ingest it and run a few queries (e.g. "Dialectical Synthesis", "Metacognition") and get relevant snippets back.

---

## Task 4: Detective Nodes (RepoInvestigator + DocAnalyst)

**Scope:** Two LangGraph nodes that output structured `Evidence`.

### Deliverables


| Deliverable                   | Requirement                                                                                                                                                                                                                                                                                                                   |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**src/nodes/detectives.py`** | **RepoInvestigator** and **DocAnalyst** as LangGraph nodes. Each consumes state (e.g. `repo_url`, `pdf_path`, cloned path or PDF chunks), calls the tools from Tasks 2 and 3, and pushes **structured `Evidence`** objects (e.g. into `state["evidences"]` via the reducer). VisionInspector is **not** required for interim. |


### Plan

1. **RepoInvestigator node**
  Input: repo URL (and possibly already-cloned path if clone is done in a prior node or in the graph entry). Use repo_tools to clone (if needed), run `extract_git_history`, `analyze_graph_structure`, and optionally state/file checks. For each "Evidence Class" you support (e.g. git history, state management, graph orchestration, safe tooling), create an `Evidence` instance (goal, found, content/location, rationale, confidence) and append to state using the reducer (e.g. by key like `"repo"` or by dimension id).
2. **DocAnalyst node**
  Input: PDF path (and/or pre-ingested chunks from doc_tools). Run queries for rubric concepts (Dialectical Synthesis, Metacognition, etc.) and cross-reference with repo evidence if available. Emit `Evidence` objects for "Theoretical Depth" and "Report Accuracy" (or similar) into state.
3. **Structured output only**
  No free-form text in state for detective outputs; only `Evidence` (or lists thereof) keyed so that the reducer doesn't overwrite (e.g. `evidences: Dict[str, List[Evidence]]` with `operator.ior`).
4. **Idempotency / errors**
  If clone or PDF fails, still return `Evidence` with `found=False` and a clear rationale rather than raising and breaking the graph.

**Done when:** Both nodes run inside a graph, read from shared state, call repo_tools and doc_tools, and write only structured `Evidence` into `state["evidences"]`.

---

## Task 5: Partial Graph (Detectives + EvidenceAggregator)

**Scope:** StateGraph with detective fan-out and one fan-in; no judges yet.

### Deliverables


| Deliverable        | Requirement                                                                                                                                                                                |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `**src/graph.py`** | Partial **StateGraph**: entry → **parallel** RepoInvestigator and DocAnalyst (fan-out) → **EvidenceAggregator** (fan-in) → end. Judges and Chief Justice are **not** required for interim. |


### Plan

1. **Entry node**
  Accept `repo_url` and `pdf_path`; optionally perform clone here and set cloned path in state, or let RepoInvestigator do it. Initialize `evidences` (and `opinions` if needed for typing) so reducers work.
2. **Fan-out to detectives**
  Use a single node that invokes RepoInvestigator and DocAnalyst in parallel (LangGraph's parallel branches: e.g. `add_conditional_edges` to two branches, or a "dispatcher" that adds both). Ensure both write into `evidences` with different keys (e.g. `"repo"`, `"doc"`) so `operator.ior` merges them.
3. **EvidenceAggregator node**
  Single node that runs after both detectives; reads `state["evidences"]`, optionally validates or normalizes, and passes through to state. No LLM required here; just aggregation/preparation for future judges.
4. **Fan-in**
  Both detective branches must converge at EvidenceAggregator (LangGraph pattern: multiple edges into the same node).
5. **End**
  From EvidenceAggregator, edge to END. No judge nodes yet.
6. **Optional**
  Conditional edges for "evidence missing" or "node failure" if you want to get credit for error handling early.

**Done when:** You can invoke the graph with a repo URL and PDF path and get a state that contains merged `evidences` from both detectives and then runs through EvidenceAggregator to end.

---

## Task 6: README and Runnable Repo

**Scope:** Others (and peers) can install and run the detective pipeline.

### Deliverables


| Deliverable     | Requirement                                                                                                                                                           |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**README.md`** | Setup (e.g. Python version, uv install, `uv sync`), how to install deps, how to run the detective graph against a target repo URL (and where to put or pass the PDF). |


### Plan

1. **Setup**
  Require Python 3.x and uv; steps: clone repo, `uv sync`, copy `.env.example` to `.env` and fill.
2. **Run**
  Document how to run the graph (e.g. `uv run python -m src.run` or `uv run python scripts/run_audit.py --repo-url <url> --pdf-path <path>`). Specify CLI args or env vars for repo URL and PDF path.
3. **Output**
  Briefly say what the run produces (e.g. state with `evidences` printed or saved to a file).

**Done when:** A fresh clone + `uv sync` + filled `.env` + one command runs the detective graph and produces evidence for a given repo + PDF.

---

## Task 7: Interim PDF Report

**Scope:** Short report that explains your choices and next steps.

### Deliverables


| Deliverable                      | Requirement                                                                                                                                                                                          |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**reports/interim_report.pdf`** | PDF committed in the repo covering: (1) architecture decisions so far, (2) known gaps and plan for judicial layer and synthesis, (3) diagrams of planned StateGraph flow (detective fan-out/fan-in). |


### Plan

1. **Architecture decisions**
  One short section each: why Pydantic/TypedDict over dicts; how AST parsing is structured (which modules, what you detect); sandboxing strategy (temp dir, subprocess, no `os.system`).
2. **Known gaps**
  List what's not done yet: judges (Prosecutor, Defense, Tech Lead), ChiefJustice, full rubric dimensions, VisionInspector (if any). For each, 1–2 sentences on how you'll implement it (e.g. "Judges will use `.with_structured_output(JudicialOpinion)` and run in parallel per criterion").
3. **Diagrams**
  One diagram showing: START → RepoInvestigator and DocAnalyst in parallel → EvidenceAggregator → (future: Judges → Chief Justice) → END. Clearly show fan-out and fan-in. Use Mermaid, draw.io, or similar; export as image and embed in PDF.
4. **Location**
  Save as `reports/interim_report.pdf` and commit so peers can access it.

**Done when:** The PDF is in the repo and covers decisions, gaps/plan, and StateGraph flow diagram.

---

## Suggested Order of Work


| Order | Task                   | Why                                                          |
| ----- | ---------------------- | ------------------------------------------------------------ |
| 1     | Task 1 (state + infra) | Everything else depends on state shape and project setup.    |
| 2     | Task 2 (repo_tools)    | RepoInvestigator node needs these first.                     |
| 3     | Task 3 (doc_tools)     | DocAnalyst node needs these.                                 |
| 4     | Task 4 (detectives)    | Implements the two nodes that produce `Evidence`.            |
| 5     | Task 5 (graph)         | Wires detectives + EvidenceAggregator; validates end-to-end. |
| 6     | Task 6 (README)        | Makes the repo runnable and submittable.                     |
| 7     | Task 7 (interim PDF)   | Document decisions and plan; diagram the graph.              |


---

## Interim Checklist (Quick Reference)

- `**src/state.py`** — Evidence, JudicialOpinion, AgentState + reducers
- `**src/tools/repo_tools.py`** — Sandboxed clone, git log, AST graph analysis
- `**src/tools/doc_tools.py**` — PDF ingest + chunked query (RAG-lite)
- `**src/nodes/detectives.py**` — RepoInvestigator + DocAnalyst nodes → Evidence
- `**src/graph.py**` — Detective fan-out → EvidenceAggregator fan-in (no judges)
- `**pyproject.toml**` — uv, locked deps
- `**.env.example**` — Required env vars, no secrets
- `**README.md**` — Install + run vs target repo URL (+ PDF)
- `**reports/interim_report.pdf**` — Decisions, gaps/plan, StateGraph diagram

