# Automaton Auditor — Current Architecture

This document describes the **current** architecture of the Automaton Auditor: graph topology, state, rubric-driven detective layer, cost controls, observability, and persistence. It records the decisions in place, the reasons behind them, and the main tradeoffs and alternatives.

---

## 1. Overview

The system is a LangGraph-based audit pipeline that:

1. Loads a rubric from `docs/rubric.json` at startup and injects it into graph state.
2. Runs three detective nodes in parallel (RepoInvestigator, DocAnalyst, VisionInspector), each as an LLM tool-calling agent that investigates only its assigned rubric dimensions.
3. Aggregates evidence into state and (in the current first stage) terminates after evidence collection. Judge and Chief Justice phases are not yet implemented.

All detective behaviour is driven by the rubric: forensic instructions, success/failure patterns, and dimension-to-artifact routing. There are no hardcoded scoring thresholds or fixed investigation sequences in the detective code.

---

## 2. Graph Topology and State

### Topology

```
START → entry → [repo_investigator, doc_analyst, vision_inspector]  (fan-out)
                     ↓                    ↓                ↓
                     └───────────────────┴────────────────┘
                                         ↓
                              evidence_aggregator → END
```

- **entry**: Validates `repo_url` and `pdf_path`, initialises `evidences` with empty buckets `repo`, `doc`, `vision`.
- **Conditional routing**: If either input is missing or invalid (e.g. URL not `https://` or `git@`), the graph routes to an **abort** node that emits `found=False` for every rubric dimension and exits. Otherwise it fans out to the three detectives.
- **Detectives**: Run in parallel; each filters `rubric_dimensions` by its `target_artifact` and runs one LLM agent loop per assigned dimension.
- **evidence_aggregator**: Ensures every dimension from the rubric has an `Evidence` in state; backfills missing ones with `found=False`. Required dimensions are derived from `rubric_dimensions` via `_required_from_rubric()` so the graph stays in sync with the rubric without code changes.

### State (`AgentState`)

Defined in `src/state.py` as a `TypedDict` with reducers so parallel nodes do not overwrite each other:

- `repo_url`, `pdf_path`: inputs.
- `rubric_dimensions`: list of dimension dicts from `rubric.json` (loaded in the CLI and passed in initial state).
- `evidences`: `Annotated[Dict[str, List[Evidence]], operator.ior]` — each detective writes into its bucket (`repo`, `doc`, `vision`); `ior` merges dicts.
- `opinions`, `final_report`: reserved for the future Judge/Chief Justice phase.

`Evidence` is a Pydantic model: `goal`, `found`, `location`, `rationale`, `confidence`.

---

## 3. Rubric and Dimension Routing

- **Source**: `docs/rubric.json`, loaded in the CLI and passed as `initial_state["rubric_dimensions"]`.
- **Per-dimension fields**: `id`, `name`, `target_artifact`, `forensic_instruction`, `success_pattern`, `failure_pattern`.

Routing is by `target_artifact`:

| `target_artifact` | Detective node        | Evidence bucket |
|-------------------|------------------------|-----------------|
| `github_repo`     | repo_investigator_node | `repo`          |
| `pdf_report`      | doc_analyst_node       | `doc`           |
| `pdf_images`      | vision_inspector_node  | `vision`        |

Each node filters `state["rubric_dimensions"]` to the dimensions it owns. No detective sees or calls tools for another detective’s dimensions. The user message for each dimension explicitly lists the tool names that node can use so the LLM does not attempt tools from another node.

---

## 4. Detective Layer

### One agent call per dimension

Each detective runs **one** `_run_detective_agent(dimension, user_message, tools, node_name)` per assigned dimension. The system prompt is built from that single dimension’s `forensic_instruction`, `success_pattern`, and `failure_pattern`. This keeps prompts small, isolates failures to a single dimension, and allows spend to be attributed per dimension (`node_name:dimension_id`).

**Alternative considered**: Passing all dimensions for that node in one big prompt. Rejected because it led to oversized prompts and rate limits, and a single dimension failure could complicate the whole response.

### Tool registries

Each node has a fixed list of LangChain `@tool` functions. Only these are bound to the LLM for that node:

- **RepoInvestigator**: `clone_repo`, `read_git_history`, `run_graph_structure_analysis`, `scan_file_for_patterns`, `list_repo_files`. The repo is cloned once upfront with `clone_repo_sandboxed`; the user message tells the LLM the `cloned_path` and instructs it not to call `clone_repo` again.
- **DocAnalyst**: `query_pdf_for_term`, `extract_and_check_file_paths`.
- **VisionInspector**: `extract_and_analyze_diagrams`.

Tools are implemented in `src/tools/` (repo_tools, doc_tools, vision_tools) and wrap deterministic logic (git, AST, PDF ingestion, path extraction). The LLM decides *what* to call and *how* to interpret results; the tools themselves remain deterministic.

### Tool-calling loop

Inside `_run_detective_agent`:

1. Build messages: system (from rubric for that dimension) + user (task + available tool names).
2. Loop for up to `_MAX_ITERATIONS` (env: `DETECTIVE_MAX_ITERATIONS`, default 8):
   - Invoke the LLM with tools; append the response to messages.
   - If there are no tool calls, break (natural completion).
   - For each tool call: apply missing-path short-circuit if applicable (see below), otherwise invoke the tool, append a `ToolMessage`, and record the result.
   - If every result in the round is “not found” or error (`_is_not_found`), increment a consecutive-empty counter; after `_MAX_EMPTY_ROUNDS` (2) such rounds, append a stop hint and break. Otherwise reset the counter.
3. Append an emission prompt asking for one `Evidence` object with explicit field names and types.
4. Use a **separate** LLM instance with `with_structured_output(Evidence)` (no tools bound) to avoid schema conflict. Retry up to 3 times on validation error; on repeated failure, return a fallback `Evidence(found=False, confidence=0.0, rationale="Evidence emission failed after 3 attempts: ...")`.

So: tool loop → single-dimension evidence → no exceptions propagated; failures become evidence.

### Missing-path short-circuit

When the tool is `scan_file_for_patterns` and the path has already been confirmed missing (e.g. a previous call returned `exists: false`), the code does **not** call the tool again. It appends a synthetic “already confirmed missing” `ToolMessage`. This avoids redundant work and extra LLM turns when the agent keeps re-requesting the same non-existent file (e.g. `src/nodes/judges.py`).

### Early exit on consecutive empty rounds

If every tool result in a round is non-productive (e.g. file not found, or `ok: false`), that round is “empty”. After `_MAX_EMPTY_ROUNDS` consecutive empty rounds, the loop injects a message telling the LLM that the target files do not exist and to proceed to emission, then breaks. So the run stops quickly when investigating missing files instead of burning iterations up to `_MAX_ITERATIONS`.

### Batching instruction

The system prompt (`_DETECTIVE_RULES`) instructs the LLM to issue **all independent** tool calls in a single response (e.g. all `query_pdf_for_term` calls for multiple terms at once). Sequential calls are only for the case where one call depends on the result of another (e.g. `list_repo_files` then `scan_file_for_patterns` on a discovered path). This reduces round-trips and keeps the system usable when the rubric grows (e.g. more terms or files to check).

---

## 5. Cost and Robustness Controls

| Mechanism              | Purpose |
|------------------------|--------|
| `DETECTIVE_MAX_ITERATIONS` (default 8) | Cap on tool-calling turns per dimension to avoid runaway loops. |
| `_MAX_EMPTY_ROUNDS` (2) | Stop after 2 consecutive rounds where every tool result is not-found/error. |
| Missing-path short-circuit | Avoid re-scanning the same absent path. |
| Batching instruction   | Fewer LLM turns per dimension. |
| One dimension per agent call | Smaller prompts, clearer attribution, isolation of failures. |

The iteration cap is a safety net; the main termination conditions are “no tool calls” (done) and “consecutive empty rounds” (nothing left to find). A higher default (e.g. 8) allows rubric growth (e.g. more terms in theoretical_depth) while early exit and short-circuit keep cost under control when files are missing.

---

## 6. Observability

### SpendTracker (`src/utils/spend_tracker.py`)

- LangChain `BaseCallbackHandler` that records token usage and estimated cost per LLM call.
- Uses **threading.local()** for the “active node” label so that when `repo_investigator` and `doc_analyst` run in parallel, each thread’s LLM calls are tagged with the correct `node_name:dimension_id`. Without this, a single shared label would mix attribution across nodes.
- Pricing is read from an internal table (e.g. Claude Haiku/Sonnet/Opus); the model name from response metadata is used to look up rates.
- At the end of a run, `TRACKER.summary()` returns totals and per-node breakdown; `TRACKER.report()` returns a human-readable string. The CLI includes spend in the evidence JSON output.

### AuditLogger (`src/utils/audit_logger.py`)

- Another callback handler that prints a step-by-step trace: which node/dimension is active, each LLM turn, each tool call (name + truncated args), each tool result (truncated), and each evidence emission.
- Also uses **threading.local()** for node name and turn counters so parallel nodes do not interleave log lines under the wrong label.
- Verbosity is toggled (e.g. via a `--verbose` flag in the CLI when wired); when off, the logger does not print.

**Decision**: Two singletons shared across the process, with thread-local state only for the “current context” (node/dimension). This keeps the API simple (pass `TRACKER` and `LOGGER` in config) while allowing correct behaviour under LangGraph’s parallel execution.

---

## 7. Vision Inspector Toggle

When `VISION_ENABLED` (env) is `false` (or `0`/`no`), `vision_inspector_node` does **not** call the LLM or any tools. It returns `found=False` evidence for all `pdf_images` dimensions with a rationale that the inspector is disabled. The check is done at **call time** via `_vision_enabled()` so that `.env` is respected even if the module is imported before `load_dotenv()`. No spend or tool execution is recorded for the vision node when disabled.

---

## 8. Evidence Aggregation

`evidence_aggregator_node` runs after all three detectives. It:

- Builds the set of required dimension IDs per bucket from `state["rubric_dimensions"]` using `_required_from_rubric()` (with a fallback to a hardcoded map if the rubric is empty).
- For each bucket, ensures every required dimension has an `Evidence`; any missing one is backfilled with `Evidence(goal=dim, found=False, location="unknown", rationale="No evidence collected — detective node did not emit this dimension.", confidence=0.0)`.

So the graph always outputs a full set of dimensions consistent with the rubric, even if a detective failed or did not emit for a dimension.

---

## 9. Checkpointing (Optional)

The graph is compiled via `make_graph(checkpointer=None)` in `src/graph.py`. If a checkpointer (e.g. LangGraph’s `SqliteSaver`) is passed, the compiled graph snapshots state after each node. A run can then be resumed by re-invoking with the same `thread_id` in `config["configurable"]`, so completed nodes are not re-run. The default `graph = make_graph()` has no checkpointer so tests and simple scripts do not need a DB.

**Current wiring**: The CLI (`main.py`) uses checkpointing by default. It creates a `SqliteSaver` at `output/checkpoints.db` (or next to `--output`), compiles the graph with `make_graph(checkpointer=...)`, and invokes with `config={"configurable": {"thread_id": "..."}}`. A new run gets an auto-generated thread ID (e.g. `audit-20260227-165030-a3f1c2`), printed at startup with a tip to re-run with `--thread-id <id>` if the process crashes. To resume, pass the same `--thread-id`; LangGraph skips completed nodes and continues from the last checkpoint.

---

## 10. Output and Persistence

- Evidence and spend are written to JSON (e.g. a timestamped file and a canonical `output/evidence.json`). The structure includes `evidences` (per bucket, list of serialised `Evidence`) and `spend` (from `TRACKER.summary()`). If checkpointing is used, `thread_id` can be stored in the same payload for resume.
- Rubric is loaded once at startup from `docs/rubric.json`; the graph does not re-read the file.

---

## 11. Decisions and Tradeoffs Summary

| Decision | Reason | Tradeoffs | Alternatives |
|----------|--------|-----------|---------------|
| **Rubric-driven detectives** | Single source of truth for what to check and what success/failure mean; no hardcoded thresholds; new dimensions only require rubric changes. | LLM must follow instructions; occasional schema/format mistakes (mitigated by emission retry and fallback). | Keep hardcoded logic per dimension (rejected: brittle, doesn’t scale with rubric). |
| **One agent call per dimension** | Small prompts, per-dimension failure isolation, clear spend attribution, lower rate-limit risk. | More LLM invocations per run than one big call per node. | One prompt per node with all dimensions (rejected: large prompts, rate limits, mixed failures). |
| **Explicit tool list in user message** | Reduces cross-node tool use (e.g. repo detective calling PDF tools). | Slightly longer user message. | Rely only on bind_tools (rejected: LLMs sometimes “hallucinate” other tools). |
| **Structured emission with separate LLM** | Clean separation from tool-calling schema; Pydantic validation. | Extra model call per dimension; possible validation failures (confidence in wrong place). | Same LLM with tool for “emit evidence” (rejected: schema conflict); or no retry (rejected: too many fallbacks). |
| **Emission retry (3) + fallback** | Handles transient schema mistakes without failing the whole dimension. | Fallback evidence is low confidence; retries add cost. | No retry (worse UX); regex fallback (possible but more brittle). |
| **Missing-path short-circuit** | Saves tool calls and tokens when the agent repeatedly asks for the same absent file. | Only applies to `scan_file_for_patterns`; path must match exactly. | No short-circuit (rejected: wasteful on missing-file dimensions). |
| **Early exit after N empty rounds** | Stops quickly when all tools return not-found/error, without using the full iteration cap. | N=2 is heuristic; could exit earlier or later. | Fixed iterations only (rejected: wastes tokens on missing files); or adaptive N (possible future improvement). |
| **Batching instruction in prompt** | Reduces turns and cost; scales with rubric size (e.g. more terms). | Depends on LLM following instructions; some calls are inherently sequential. | No batching (rejected: too many turns in practice). |
| **threading.local() in TRACKER/LOGGER** | Parallel detective nodes get correct per-thread labels and counts. | Slightly more complex than a single global. | Single global (rejected: wrong attribution when nodes run in parallel). |
| **Vision toggle at call time** | Ensures `.env` is respected and no LLM/tools run when disabled. | None. | Module-level constant (rejected: can be wrong if env not yet loaded). |
| **make_graph(checkpointer)** | Enables optional persistence and resume without changing default behaviour. | Caller must pass checkpointer and thread_id when desired. | Always use checkpointer (rejected: tests/simple use don’t need it). |
| **Required dimensions from rubric** | Aggregator and abort node stay in sync with rubric; no code change for new dimensions. | Fallback to hardcoded map if rubric missing. | Always hardcode (rejected: duplicates rubric). |

---

## 12. Current Limitations and Not Yet Implemented

- **Judge layer**: Prosecutor, Defense, Tech Lead nodes and Chief Justice synthesis are not implemented. The rubric references them; the graph currently ends at evidence aggregation.
- **Incremental evidence persistence**: Evidence is written only after the full run. If the process crashes mid-run, evidence collected so far is lost unless checkpointing is used and the run is resumed with the same `thread_id`.
- **CLI**: Checkpointing and `--thread-id` are wired in `main.py`; the graph is always run with a checkpointer so resume is available after a crash.
- **Pydantic emission failures**: In practice the LLM sometimes puts `confidence` inside the rationale (e.g. XML-like). The retry and fallback handle this but at the cost of extra calls or a low-confidence fallback; a future improvement could be a stricter prompt or a small regex-based recovery.

This document reflects the architecture as implemented in the codebase at the time of writing. For the original design narrative and option analysis that led to the rubric-driven detective design, see `docs/explanation/rubric-driven-detectives.md`.
