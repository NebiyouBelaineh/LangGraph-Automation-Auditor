# Rubric-Driven Detective Architecture

This document describes how the detective layer of the Automaton Auditor can be redesigned so that each detective agent reads the rubric at runtime, decides which tools to call based on its assigned forensic instructions, and emits structured `Evidence` objects — without any hardcoded scoring thresholds or fixed investigation sequences in the detective node code itself.

---

## The Problem with the Current Approach

The current detective nodes in `src/nodes/detectives.py` are **rubric-blind**. Each function (`repo_investigator_node`, `doc_analyst_node`) has its investigation logic, tool calls, and confidence thresholds hardcoded in Python:

```python
# Current: hardcoded thresholds, fixed investigation sequence
confidence=0.85 if git_result.progression_detected else 0.4
```

This means:

- Adding a new rubric dimension (e.g., `structured_output_enforcement`) requires editing `detectives.py`.
- Changing what counts as "success" for a dimension requires editing the Python code, not the rubric.
- The detective has no awareness of `forensic_instruction`, `success_pattern`, or `failure_pattern` from `rubric.json` — it can never explain *why* it drew the conclusions it did in terms the rubric understands.
- The `rubric_dimensions` field already exists in `AgentState` but is never populated or read by any detective node.

---

## The Target Architecture: LLM Tool-Calling Detective

In the target architecture, each detective is an **LLM agent** that:

1. Receives a list of rubric dimensions assigned to it (from `AgentState.rubric_dimensions`).
2. Is given a set of Python tool functions it can call.
3. Reads the `forensic_instruction` for each dimension and autonomously decides which tools to invoke, in what order, and how to interpret the results.
4. Emits a structured `Evidence` object for each dimension it investigated.

The rubric becomes the detective's "case brief". The Python tools remain deterministic and side-effect-free; only the *decision of what to call and how to judge the result* moves into the LLM.

---

## How Rubric Dimensions Are Routed

The `rubric.json` file already partitions dimensions by `target_artifact`:


| `target_artifact` | Assigned to        |
| ----------------- | ------------------ |
| `"github_repo"`   | `RepoInvestigator` |
| `"pdf_report"`    | `DocAnalyst`       |
| `"pdf_images"`    | `VisionInspector`  |


At startup (in `main.py` or `entry_node`), `rubric.json` is loaded and written into `AgentState.rubric_dimensions`. Each detective node filters this list by its own `target_artifact` value to get its work assignment. No detective sees dimensions that belong to another detective.

```
rubric.json loaded at startup
        │
        ▼
AgentState.rubric_dimensions = [all 10 dimensions]
        │
        ├── RepoInvestigator filters: target_artifact == "github_repo"
        │       → [git_forensic_analysis, state_management_rigor,
        │           graph_orchestration, safe_tool_engineering,
        │           structured_output_enforcement, judicial_nuance,
        │           chief_justice_synthesis]
        │
        ├── DocAnalyst filters: target_artifact == "pdf_report"
        │       → [theoretical_depth, report_accuracy]
        │
        └── VisionInspector filters: target_artifact == "pdf_images"
                → [swarm_visual]
```

---

## The Tool Registry

Each detective has a **tool registry** — a curated set of Python functions wrapped as LangChain/LangGraph tools. The LLM can call any tool in its registry zero or more times per dimension.

The existing tool implementations in `src/tools/` map cleanly to this model:

### RepoInvestigator Tool Registry


| Tool name                 | Wraps                           | What it does                                           |
| ------------------------- | ------------------------------- | ------------------------------------------------------ |
| `clone_repo`              | `clone_repo_sandboxed()`        | Clones the repo to a temp dir; returns the cloned path |
| `read_git_history`        | `extract_git_history()`         | Returns commit list, count, and detected phases        |
| `scan_state_file`         | AST scan of `src/state.py`      | Returns class names, Pydantic usage, reducer usage     |
| `analyze_graph_structure` | `analyze_graph_structure()`     | Returns nodes, edges, fan-out/fan-in detection         |
| `scan_file_for_patterns`  | General file reader + regex/AST | Returns whether patterns exist in a given file path    |
| `list_directory`          | `Path.rglob`                    | Lists files matching a glob in the cloned repo         |


### DocAnalyst Tool Registry


| Tool name               | Wraps                            | What it does                                      |
| ----------------------- | -------------------------------- | ------------------------------------------------- |
| `ingest_pdf`            | `ingest_pdf()`                   | Parses PDF into text chunks                       |
| `query_pdf_chunks`      | `query_pdf()`                    | TF-scores chunks against a query string           |
| `extract_file_paths`    | `extract_file_paths_from_text()` | Finds all `src/...` file path mentions in the PDF |
| `cross_reference_paths` | Path existence check             | Checks which mentioned paths actually exist       |


### VisionInspector Tool Registry


| Tool name         | Wraps                       | What it does                                                         |
| ----------------- | --------------------------- | -------------------------------------------------------------------- |
| `extract_images`  | `extract_images_from_pdf()` | Pulls images from the PDF                                            |
| `analyze_diagram` | `analyze_diagram()`         | Sends an image to a vision LLM and gets classification + description |


---

## The Detective Node Structure

Each detective node follows the same three-stage structure:

```
┌─────────────────────────────────────────────────────┐
│  Stage 1: Build the agent                           │
│  • Filter rubric_dimensions by target_artifact      │
│  • Construct system prompt from forensic rubric     │
│  • Bind tool registry to LLM                        │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  Stage 2: Run the agent loop (per dimension)        │
│  • LLM reads forensic_instruction                   │
│  • LLM calls tools (clone, scan, query, …)          │
│  • LLM receives tool outputs                        │
│  • LLM decides whether to call more tools           │
│  • Loop ends when LLM emits a final answer          │
└─────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│  Stage 3: Emit Evidence                             │
│  • Parse LLM final response into Evidence schema    │
│  • Write to state["evidences"]["repo"|"doc"|"vision"]│
└─────────────────────────────────────────────────────┘
```

---

## System Prompt Construction

The system prompt is assembled dynamically from the rubric at runtime. It has three parts:

### Part 1: Role and Rules (static per detective type)

```
You are a forensic investigator auditing a software repository.
Your job is to collect factual evidence for each assigned rubric dimension.

Rules:
- Call tools to gather evidence. Do not make assumptions without tool evidence.
- For each dimension, emit exactly one Evidence object with:
    goal: the dimension id
    found: true if evidence exists, false otherwise
    content: a short factual summary of what was found
    location: the file path, commit hash, or tool name where evidence was found
    rationale: explain your confidence in terms of the success/failure pattern
    confidence: a float between 0.0 and 1.0
- Do not invent file contents. If a tool returns an error, set found=false.
```

### Part 2: Dimension Briefs (generated per run from rubric.json)

For each dimension assigned to this detective, one block is injected:

```
--- Dimension: git_forensic_analysis ---
Forensic Instruction:
  Run 'git log --oneline --reverse' on the cloned repository. Count commits.
  Check if the commit history tells a progression story: Environment Setup ->
  Tool Engineering -> Graph Orchestration. Flag if there is a single 'init'
  commit or a 'bulk upload' pattern.

Success Pattern:
  More than 3 commits showing clear progression. Atomic, step-by-step history
  with meaningful commit messages.

Failure Pattern:
  Single 'init' commit or bulk upload. Timestamps clustered within minutes.
```

### Part 3: Output Format Instruction (static)

```
For each dimension, output a JSON object matching this schema:
{
  "goal": "<dimension_id>",
  "found": <true|false>,
  "content": "<brief factual summary>",
  "location": "<file path or tool name>",
  "rationale": "<explanation referencing success/failure pattern>",
  "confidence": <0.0 to 1.0>
}

Output one JSON object per dimension, separated by newlines.
Do not include markdown fences.
```

---

## A Concrete Execution Trace

Here is what the `RepoInvestigator` agent does when investigating the `graph_orchestration` dimension:

```
[LLM receives system prompt with forensic_instruction for graph_orchestration]

LLM → tool call: clone_repo(url="https://github.com/...")
Tool → { "ok": true, "cloned_path": "/tmp/auditor_clone_xyz" }

LLM → tool call: analyze_graph_structure(repo_path="/tmp/auditor_clone_xyz")
Tool → {
  "ok": true,
  "nodes": ["entry","repo_investigator","doc_analyst","vision_inspector","evidence_aggregator"],
  "edges": [["entry","repo_investigator"],["entry","doc_analyst"],["entry","vision_inspector"],
            ["repo_investigator","evidence_aggregator"],["doc_analyst","evidence_aggregator"],
            ["vision_inspector","evidence_aggregator"]],
  "has_parallel_branches": true,
  "has_fan_in": true,
  "conditional_edges_count": 1
}

LLM → (no more tool calls needed for this dimension)

LLM emits Evidence:
{
  "goal": "graph_orchestration",
  "found": true,
  "content": "5 nodes detected. Fan-out from entry to 3 detectives. Fan-in at evidence_aggregator. 1 conditional edge.",
  "location": "src/graph.py",
  "rationale": "Parallel branches exist (fan-out: true) and a synchronization node exists (fan-in: true), matching the success pattern. However, judge fan-out/fan-in is absent — the graph ends at evidence_aggregator. Partial credit.",
  "confidence": 0.6
}
```

Notice what changed compared to the current implementation: the `rationale` now **cites the success and failure patterns from the rubric** rather than just reporting raw booleans, and the confidence reflects the *partial* match against the rubric's full criteria rather than a binary 0.9 / 0.5 threshold.

---

## Structured Output Enforcement for Evidence Emission

The `Evidence` Pydantic model is used as the structured output schema for the detective LLM's final response, using `.with_structured_output()`:

```python
from langchain_anthropic import ChatAnthropic
from src.state import Evidence

llm = ChatAnthropic(model="claude-3-5-haiku-20241022")
llm_with_tools = llm.bind_tools(tool_registry)
evidence_emitter = llm.with_structured_output(Evidence)
```

The agent loop uses `llm_with_tools` for the reasoning/tool-calling turns, and switches to `evidence_emitter` for the final emission turn once it has gathered enough evidence. This prevents freeform text from leaking into state as malformed Evidence objects.

If the structured output parse fails, the node catches the error and falls back to `Evidence(found=False, confidence=0.0, rationale="structured_output_parse_failed: ...")` rather than crashing.

---

## Changes Required to the Existing Codebase


| File                        | Change                                                                   |
| --------------------------- | ------------------------------------------------------------------------ |
| `main.py`                   | Load `docs/rubric.json` and add to initial state as `rubric_dimensions`  |
| `src/state.py`              | No changes needed — `rubric_dimensions: List[Dict]` field already exists |
| `src/tools/repo_tools.py`   | Wrap each function with `@tool` decorator (LangChain) or equivalent      |
| `src/tools/doc_tools.py`    | Same — wrap `ingest_pdf`, `query_pdf`, `extract_file_paths_from_text`    |
| `src/tools/vision_tools.py` | Same — wrap `extract_images_from_pdf`, `analyze_diagram`                 |
| `src/nodes/detectives.py`   | Replace hardcoded logic with LLM agent loop for each detective           |
| `src/graph.py`              | No structural changes — graph topology stays the same                    |


The tool wrapping is the smallest change. The bulk of the work is rewriting `detectives.py` to:

1. Filter rubric dimensions from state.
2. Build the system prompt dynamically.
3. Run the tool-calling agent loop.
4. Parse and emit Evidence.

---

## Tradeoffs

### Flexibility vs. Predictability

The primary advantage of this architecture is that the investigation logic lives in the rubric, not in Python. Updating what constitutes evidence for a dimension only requires editing `rubric.json`. The tradeoff is that the LLM may occasionally miscall tools, skip a tool it should have used, or draw a different conclusion than a deterministic function would — making the system harder to unit-test.

**Mitigation:** Keep all tools deterministic and side-effect-free. The LLM only decides *which* tools to call and *how to interpret* the results. This isolates non-determinism to the reasoning layer only.

### Latency

Three LLM agents running in parallel (fan-out is already in the graph) will add latency over the current purely local execution. Each detective may make 3–6 tool calls per dimension, and each tool call is a round-trip to the LLM to decide whether to call another tool.

**Mitigation:** Use a fast, cheap model (e.g. `claude-3-5-haiku` or `gpt-4o-mini`) for the detective tool-calling loop. The detective agents do not need deep reasoning — they need reliable tool dispatch and structured output. Reserve the more capable model for the Judicial layer.

### Cost

More LLM calls means higher API cost per audit run. Each of the three detectives runs for multiple dimensions (7 for Repo, 2 for Doc, 1 for Vision), and each dimension may take multiple tool-call turns.

**Mitigation:** Cache the clone result and PDF chunks at the start of each detective run so tools are only called once regardless of how many dimensions need them.

### Why Not Keep It Fully Deterministic?

A purely deterministic approach (the current code) is fast and predictable, but it cannot:

- Evaluate the *quality* of commit messages, not just count them.
- Judge whether a term appears "substantively" in the PDF vs. as a buzzword.
- Read a file it was not specifically coded to read and extract meaningful patterns.

The rubric's `forensic_instruction` fields describe what a *human investigator* would do — they require judgment, not just pattern matching. An LLM agent that can freely call tools is the appropriate mechanism for executing those instructions.

---

## Summary

The rubric-driven detective architecture replaces hardcoded Python investigation logic with LLM agents that read their case brief (rubric dimensions with `forensic_instruction`) at runtime and autonomously call tools to collect evidence. The key design decisions are:

- **Routing** by `target_artifact` assigns dimensions to the right detective without code changes.
- **System prompt construction** injects `forensic_instruction`, `success_pattern`, and `failure_pattern` so the LLM's rationale is grounded in the rubric.
- **Structured output enforcement** via `.with_structured_output(Evidence)` prevents malformed evidence from entering state.
- **Deterministic tools** ensure that the non-determinism is confined to the reasoning layer, not the data collection layer.
- **Existing tools** in `src/tools/` require only decorator wrapping — their implementation is already well-suited to this model.

