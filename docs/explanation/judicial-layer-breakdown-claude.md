# Judicial Layer — Requirements, Tasks & Deliverables

**Source:** [TRP1 Challenge Week 2: The Automaton Auditor](../TRP1%20Challenge%20Week%202_%20The%20Automaton%20Auditor%20(1).md)

---

## Context: Where the Graph Currently Stands

The graph ends at `evidence_aggregator → END`. Every rubric dimension has a collected `Evidence` object in `state["evidences"]`. The judicial layer picks up from there.

```
Current:
  START → entry → [repo_investigator, doc_analyst, vision_inspector] → evidence_aggregator → END

Target:
  START → entry → [repo_investigator, doc_analyst, vision_inspector] → evidence_aggregator
                                                                             ↓
                                              [Prosecutor, Defense, TechLead] (fan-out)
                                                             ↓
                                                      chief_justice (fan-in)
                                                             ↓
                                                            END
```

The Pydantic models for the judicial layer (`JudicialOpinion`, `CriterionResult`, `AuditReport`) and the state reducers (`opinions: Annotated[List[JudicialOpinion], operator.add]`, `final_report`) are already defined in `src/state.py`. The task is to implement the nodes and wire the graph.

---

## Layer 2 — The Bench: Three Judges

### Core concept

This is a **Dialectical Process** (Thesis → Antithesis → Synthesis). For **every** rubric dimension, the **same** evidence is passed to **three** judge personas that run **in parallel** and produce three **independent** `JudicialOpinion` objects. These opinions will conflict — that conflict is intentional. The Chief Justice resolves it.

### The three personas

#### Prosecutor — "Trust No One. Assume Vibe Coding."

**Goal:** Scrutinize evidence for gaps, security flaws, and laziness. Argue for the lowest defensible score.

**What to look for:**
- Linear graph flow where parallelism was required → charge: **"Orchestration Fraud"** → max score = 1 for `graph_orchestration`.
- Judge nodes returning freeform text with no Pydantic validation → charge: **"Hallucination Liability"** → max score = 2 for `judicial_nuance`.
- Unsanitized `os.system()` calls, no error handling → charge: **"Security Negligence"**.
- Files/features the PDF report claims exist that the repo does not have → charge: **"Report Hallucination"**.

**Output shape:** Harsh score (typically 1–2 for violations), a list of specific missing elements, and exact failure evidence.

**Prompt mandate:** Include adversarial language. Explicitly instruct the model to find gaps, not strengths. Reference the Prosecutor's Handbook (Protocol B, Statute of Orchestration and Statute of Engineering) from the rubric.

---

#### Defense Attorney — "Reward Effort and Intent. Look for the Spirit of the Law."

**Goal:** Highlight creative workarounds, effort visible in git history, and architectural understanding even when implementation has bugs.

**What to look for:**
- Graph fails to compile due to a minor edge error, but AST parsing logic is sophisticated (extracts imports without regex) → argue for score = 3 for `safe_tool_engineering` despite broken graph.
- Chief Justice synthesis node is an LLM prompt instead of hardcoded rules, but all three judge personas are distinct and actively disagree → argue for score 3–4 for `judicial_nuance` ("Role Separation achieved dialectical tension").
- Git history shows struggle and iteration (many commits, meaningful messages) → argue for higher score on `git_forensic_analysis`.

**Output shape:** Generous score (typically 3–5), highlight of specific strengths, mitigating circumstances.

**Prompt mandate:** Instruct the model to reward intent and effort. Reference the Defense Attorney's Handbook (Protocol B, Statute of Effort).

---

#### Tech Lead — "Does it actually work? Is it maintainable?"

**Goal:** Evaluate architectural soundness and practical viability. Ignore vibe and ignore struggle. Focus on the artifacts. Act as tie-breaker between Prosecutor and Defense.

**What to look for:**
- `operator.add` / `operator.ior` reducers used to prevent state overwrites → confirms architectural rigor.
- `tempfile.TemporaryDirectory()` used for cloning → confirms sandboxing standard.
- Plain Python dicts used for state (no Pydantic) → ruling: **"Technical Debt"** → score = 3 (works but brittle).
- `os.system('git clone <url>')` with no sandbox → ruling: **"Security Negligence"** → overrides all effort points for forensic accuracy.

**Output shape:** Realistic score — specifically 1, 3, or 5 only (no 2 or 4) according to the challenge spec — plus technical remediation advice (specific file-level instructions).

**Prompt mandate:** Focus only on artifacts, not effort. Reference the Tech Lead's Handbook (Protocol B, Statute of Engineering).

---

### What a judge invocation looks like per dimension

For each rubric dimension (e.g. `state_management_rigor`):

```
Input to each judge:
  - dimension: { id, name, success_pattern, failure_pattern, forensic_instruction }
  - evidence: Evidence(goal="state_management_rigor", found=True/False, location="...", rationale="...", confidence=0.9)

Parallel output:
  - Prosecutor:  JudicialOpinion(judge="Prosecutor",  criterion_id="state_management_rigor", score=1, argument="...", cited_evidence=[...])
  - Defense:     JudicialOpinion(judge="Defense",     criterion_id="state_management_rigor", score=4, argument="...", cited_evidence=[...])
  - TechLead:    JudicialOpinion(judge="TechLead",    criterion_id="state_management_rigor", score=3, argument="...", cited_evidence=[...])
```

All three opinions go into `state["opinions"]` via the `operator.add` reducer. After all dimensions are processed: `len(state["opinions"]) == 3 × num_dimensions`.

---

### Structured output requirement (non-negotiable)

Judges **must not** return freeform text. The challenge treats freeform text as a validator error.

```python
# Required pattern
llm = ChatAnthropic(...)
judge_llm = llm.with_structured_output(JudicialOpinion)

# On validation failure → retry up to N times → fallback JudicialOpinion(score=1, argument="Emission failed", ...)
```

This mirrors the same emission pattern already used in `detectives.py` for `Evidence` emission.

---

## Layer 3 — The Supreme Court: Chief Justice

### Core concept

The Chief Justice is **not** an LLM judge. It is a **deterministic Python function** that receives all `JudicialOpinion` objects, groups them by `criterion_id`, and applies named hard-coded rules to resolve conflicts and produce the final score for each dimension. The challenge is explicit: averaging the scores or using another LLM prompt here = failure mode (score capped at 2 for `chief_justice_synthesis` criterion).

### Conflict resolution rules (from `docs/rubric.json` → `synthesis_rules`)

These must be implemented as Python `if/else` logic:

| Rule name | Trigger | Effect |
|---|---|---|
| **security_override** | Prosecutor identifies a confirmed security flaw (e.g. `os.system` with unsanitized input, shell injection) | Cap final score at **3** for that criterion. Overrides any effort points the Defense argued. |
| **fact_supremacy** | Defense argues a feature exists ("Deep Metacognition", "parallel judges") but Detective evidence (`found=False`) contradicts it | Overrule Defense. Evidence facts beat judicial opinion. |
| **functionality_weight** | Tech Lead confirms architecture is modular and workable | Tech Lead's view carries **highest weight** for architecture-type criteria (e.g. `graph_orchestration`). |
| **dissent_requirement** | Score variance across three judges **> 2** (e.g. P=1, D=5) | Must produce an explicit **dissent summary** in `CriterionResult.dissent_summary` explaining why they disagreed. |
| **variance_re_evaluation** | Score variance **> 2** | Trigger re-evaluation of the specific evidence each judge cited before rendering the final score. (Can be implemented as a second pass through the evidence with the same rules — no new LLM call needed.) |

### Output

The Chief Justice produces one `CriterionResult` per dimension and assembles them into an `AuditReport`:

```python
AuditReport(
    repo_url=state["repo_url"],
    executive_summary="...",        # overall verdict narrative
    overall_score=float,            # e.g. mean of all final_scores
    criteria=[CriterionResult(...)], # one per dimension
    remediation_plan="...",         # consolidated remediation from all criteria
)
```

Each `CriterionResult`:
```python
CriterionResult(
    dimension_id="graph_orchestration",
    dimension_name="Graph Orchestration Architecture",
    final_score=3,                  # after applying conflict resolution rules
    judge_opinions=[...],           # all three JudicialOpinion objects for this dimension
    dissent_summary="...",          # required if variance > 2, else None
    remediation="Add parallel fan-out in src/graph.py: ...",  # file-level instructions
)
```

---

## Files to Create

### `src/nodes/judges.py`

**Purpose:** Implement the three judge nodes (or one parameterized function).

**Key decisions to make:**

**Option A — Three separate LangGraph nodes (simplest to wire in graph):**
- `prosecutor_node(state)` — iterates over all dimensions, produces N opinions.
- `defense_node(state)` — same, different persona prompt.
- `tech_lead_node(state)` — same, different persona prompt.
- All three run in parallel in the graph; `operator.add` merges their opinion lists.

**Option B — One dispatcher node that loops dimensions and parallelizes internally:**
- `judge_dispatcher_node(state)` — for each dimension, calls all three judge LLMs (can be done concurrently with `asyncio` or sequentially), appends 3 opinions per dimension.
- Single node in the graph, simpler wiring, but less visible in LangSmith trace per persona.

The challenge spec says the graph must show the three personas running in parallel (for the LangSmith trace and `graph_orchestration` score), so **Option A** is safer for rubric compliance.

**Structure inside each judge function:**
1. Extract the evidence for each dimension from `state["evidences"]` (flattten the buckets or look up by `goal`).
2. For each dimension, build a system prompt (persona-specific) + user prompt (dimension + evidence JSON).
3. Call `llm.with_structured_output(JudicialOpinion)`.
4. Retry on `ValidationError` (up to 3 times); fallback `JudicialOpinion` on total failure.
5. Return `{"opinions": [list of JudicialOpinion for all dimensions]}`.

**Persona system prompt template inputs:**
- `forensic_instruction`, `success_pattern`, `failure_pattern` from the dimension dict (already in `rubric_dimensions`).
- The applicable Statute from Protocol B (Prosecutor's Handbook, Defense Handbook, Tech Lead Handbook) — hard-code into each persona's system prompt or load from rubric.

---

### `src/nodes/justice.py`

**Purpose:** Implement `chief_justice_node` — pure Python, no LLM.

**Skeleton:**

```python
def chief_justice_node(state: AgentState) -> dict:
    opinions: list[JudicialOpinion] = state["opinions"]
    evidences: dict = state["evidences"]
    rubric_dimensions: list[dict] = state["rubric_dimensions"]

    # 1. Group opinions by criterion_id
    by_criterion: dict[str, list[JudicialOpinion]] = {}
    for op in opinions:
        by_criterion.setdefault(op.criterion_id, []).append(op)

    criteria_results: list[CriterionResult] = []

    for dim in rubric_dimensions:
        dim_id = dim["id"]
        dim_opinions = by_criterion.get(dim_id, [])

        # 2. Apply conflict resolution rules
        final_score = _resolve(dim_id, dim_opinions, evidences)
        dissent = _dissent_summary(dim_opinions) if _variance(dim_opinions) > 2 else None
        remediation = _build_remediation(dim_id, dim_opinions, evidences)

        criteria_results.append(CriterionResult(
            dimension_id=dim_id,
            dimension_name=dim["name"],
            final_score=final_score,
            judge_opinions=dim_opinions,
            dissent_summary=dissent,
            remediation=remediation,
        ))

    overall = sum(c.final_score for c in criteria_results) / len(criteria_results) if criteria_results else 0.0

    report = AuditReport(
        repo_url=state["repo_url"],
        executive_summary=_executive_summary(criteria_results, overall),
        overall_score=round(overall, 2),
        criteria=criteria_results,
        remediation_plan=_remediation_plan(criteria_results),
    )

    return {"final_report": report}
```

**Rule helpers to implement:**
- `_variance(opinions)` → `max(scores) - min(scores)`.
- `_resolve(dim_id, opinions, evidences)` → apply security_override, fact_supremacy, functionality_weight, then weighted median or Tech Lead score as default.
- `_dissent_summary(opinions)` → text explaining the conflict.
- `_build_remediation(dim_id, opinions, evidences)` → take the Tech Lead's `argument` + Prosecutor's cited gaps, format as file-level instructions.

---

### Graph wiring additions in `src/graph.py`

After the existing `evidence_aggregator` node:

```python
# Import judge and justice nodes
from src.nodes.judges import prosecutor_node, defense_node, tech_lead_node
from src.nodes.justice import chief_justice_node
from src.state import JudicialOpinion

# Add nodes
builder.add_node("prosecutor",    prosecutor_node)
builder.add_node("defense",       defense_node)
builder.add_node("tech_lead",     tech_lead_node)
builder.add_node("chief_justice", chief_justice_node)

# Fan-out from evidence_aggregator to the three judges in parallel
builder.add_edge("evidence_aggregator", "prosecutor")
builder.add_edge("evidence_aggregator", "defense")
builder.add_edge("evidence_aggregator", "tech_lead")

# Fan-in: all three judges converge at chief_justice
builder.add_edge("prosecutor",  "chief_justice")
builder.add_edge("defense",     "chief_justice")
builder.add_edge("tech_lead",   "chief_justice")

# Chief justice → END
builder.add_edge("chief_justice", END)
```

Also **remove** (or update) the existing `builder.add_edge("evidence_aggregator", END)` line — the graph no longer ends there.

---

### Markdown serialization

Add a helper (e.g. `src/utils/report_writer.py` or inline in `justice.py`):

```python
def audit_report_to_markdown(report: AuditReport) -> str:
    ...
```

Structure:
```
# Audit Report: <repo_url>

## Executive Summary
<overall_score> / 5.0

<executive_summary text>

---

## Criterion Breakdown

### 1. <dimension_name> — Score: <final_score>/5

**Prosecutor:** score=X — <argument>
**Defense:** score=X — <argument>
**Tech Lead:** score=X — <argument>

**Dissent:** <dissent_summary>  ← only when variance > 2

**Remediation:** <remediation>

---

## Remediation Plan
<remediation_plan>
```

In `main.py`, after `graph.invoke(...)`:
```python
if state.get("final_report"):
    md = audit_report_to_markdown(state["final_report"])
    Path("output/audit_report.md").write_text(md)
```

---

## Deliverables Checklist (Final Submission)

| File / Artifact | Requirement |
|---|---|
| `src/nodes/judges.py` | Prosecutor, Defense, TechLead with **distinct** system prompts. `.with_structured_output(JudicialOpinion)`. Retry on parse failure. Run in parallel in the graph. |
| `src/nodes/justice.py` | `chief_justice_node` with **hardcoded** deterministic Python rules (security_override, fact_supremacy, functionality_weight, dissent_requirement, variance_re_evaluation). Returns `AuditReport`. |
| `src/graph.py` | Full graph: detectives → evidence_aggregator → **[Prosecutor, Defense, TechLead]** → **chief_justice** → END. |
| `output/audit_report.md` | Markdown report from `AuditReport`: Executive Summary → Criterion Breakdown (3 opinions + dissent + remediation per dimension) → Remediation Plan. |
| `audit/report_onself_generated/` | Markdown report from running agent against your own repo. |
| `audit/report_onpeer_generated/` | Markdown report from running agent against assigned peer's repo. |
| `audit/report_bypeer_received/` | The report your peer's agent generated when auditing your repo. |

---

## What the Rubric Scores for This Work

| Criterion | Score 1 | Score 3 | Score 5 |
|---|---|---|---|
| **Judicial Nuance** | Single "Grader" agent, no personas. | Prosecutor and Defense roles exist, different viewpoints, but synthesis is a simple average or pure LLM. | Three genuinely conflicting judges. Final verdict from deterministic rules. Explains *why* one side was overruled. |
| **LangGraph Architecture** | Linear script, no state management. | Typed state + error handling + structured JSON from Pydantic. | Parallel execution for detectives **and** judges. Reducers. Strict typing. |
| **Report Quality** | Generic text, no file paths, no actionable advice. | Lists missing files, basic advice. | Detailed remediation plan, file-level instructions, explains "why" with Dialectical Synthesis reference. |

The rubric specifically checks `src/nodes/judges.py` and `src/nodes/justice.py` by name. A missing or empty file in those paths will result in `found=False` evidence from RepoInvestigator when peers run their auditors against your repo.
