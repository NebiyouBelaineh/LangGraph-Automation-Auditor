# Atomic commit plan (from feat/tool-engineering)

**Excluded from all commits:** `.cursor/`, `output/` (generated artifacts).

**Optional:** Add `output/` to `.gitignore` before committing (recommended).

---

## Strategy: one branch, linear history

Do all commits on the current branch `feat/tool-engineering`. Each block below is one commit; run the commands in order and approve as you go.

---

### 0. (Optional) Ignore output directory

```bash
echo "output/" >> .gitignore
git add .gitignore
git commit -m "chore: ignore output directory for evidence JSON"
```

---

### 1. Dependencies

**What:** Add LangGraph, vision, and PDF deps to pyproject; lock with uv.

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add dependencies for LangGraph, vision, and PDF (anthropic, openai, pymupdf, pytest)"
```

---

### 2. Repo tools

**What:** Sandboxed clone, git history, and AST graph analysis for RepoInvestigator.

```bash
git add src/tools/repo_tools.py
git commit -m "feat(tools): add repo_tools (clone_sandboxed, git history, graph analysis)"
```

---

### 3. Doc tools

**What:** PDF ingest, TF-ranked query, and file-path extraction for DocAnalyst.

```bash
git add src/tools/doc_tools.py
git commit -m "feat(tools): add doc_tools (PDF ingest, query, path extraction)"
```

---

### 4. Vision tools

**What:** Image extraction from PDF and multimodal diagram classifier for VisionInspector.

```bash
git add src/tools/vision_tools.py
git commit -m "feat(tools): add vision_tools (extract images, analyze diagram)"
```

---

### 5. Tools package API

**What:** Export repo, doc, and vision tools from src.tools.

```bash
git add src/tools/__init__.py
git commit -m "feat(tools): export repo, doc, and vision tools from package"
```

---

### 6. Detective nodes

**What:** RepoInvestigator, DocAnalyst, VisionInspector nodes using the new tools.

```bash
git add src/nodes/detectives.py
git commit -m "feat(nodes): add detective nodes (RepoInvestigator, DocAnalyst, VisionInspector)"
```

---

### 7. LangGraph definition

**What:** StateGraph with entry_node, three detective branches, evidence_aggregator, conditional abort.

```bash
git add src/graph.py
git commit -m "feat(graph): add LangGraph with entry, detectives, evidence aggregator"
```

---

### 8. CLI entrypoint

**What:** main.py with --repo-url, --pdf-path, --output, --help-docker and graph invocation.

```bash
git add main.py
git commit -m "feat(cli): add main.py CLI (repo-url, pdf-path, output, docker help)"
```

---

### 9. Environment example

**What:** .env.example for vision/LLM keys (Anthropic/OpenAI).

```bash
git add .env.example
git commit -m "chore: add .env.example for vision/LLM API keys"
```

---

### 10. README

**What:** Architecture, setup, env vars, usage, project structure, known gaps.

```bash
git add README.md
git commit -m "docs: update README with architecture, setup, and usage"
```

---

### 11. Docker

**What:** Dockerfile and .dockerignore for container runs.

```bash
git add Dockerfile .dockerignore
git commit -m "chore: add Dockerfile and .dockerignore for container runs"
```

---

### 12. Interim docs

**What:** Explanation, report, and interim plan under docs/.

```bash
git add docs/
git commit -m "docs: add interim docs (architecture, plan, explanation)"
```

---

### 13. Tests

**What:** Tests for tools, detectives, and graph.

```bash
git add tests/
git commit -m "test: add tests for tools, detectives, and graph"
```

---

## Alternative: save work on feature branches

If you want to preserve each feature on its own branch (e.g. for PRs or rollback), create branches from `feat/tool-engineering` **before** making the first commit, then do the commits on the right branch and merge back.

1. **Create branches (from current branch, before any new commits):**

```bash
git checkout -b chore/deps feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b feat/tools-repo feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b feat/tools-doc feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b feat/tools-vision feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b feat/tools-api feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b feat/nodes-detectives feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b feat/graph feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b feat/cli feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b chore/env-docs feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b chore/docker feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b docs/interim feat/tool-engineering
git checkout feat/tool-engineering
git checkout -b test/suite feat/tool-engineering
git checkout feat/tool-engineering
```

2. **Then apply commits on the corresponding branch** (e.g. commit 1 on `chore/deps`, commit 2 on `feat/tools-repo`, …), and merge into `feat/tool-engineering` when you want:

```bash
git checkout feat/tool-engineering
git merge chore/deps --no-edit
git merge feat/tools-repo --no-edit
# ... etc.
```

**Simpler option:** Use 3–4 branches and group commits:

| Branch | Commits (by number above) |
|--------|---------------------------|
| `chore/setup` | 0, 1, 9, 11 |
| `feat/auditor-core` | 2, 3, 4, 5, 6, 7, 8 |
| `docs/auditor` | 10, 12 |
| `test/auditor` | 13 |

Create and use them like this:

```bash
git checkout -b chore/setup feat/tool-engineering
# run commits 0, 1, 9, 11
git checkout feat/tool-engineering
git merge chore/setup --no-edit

git checkout -b feat/auditor-core feat/tool-engineering
# run commits 2, 3, 4, 5, 6, 7, 8
git checkout feat/tool-engineering
git merge feat/auditor-core --no-edit

git checkout -b docs/auditor feat/tool-engineering
# run commits 10, 12
git checkout feat/tool-engineering
git merge docs/auditor --no-edit

git checkout -b test/auditor feat/tool-engineering
# run commit 13
git checkout feat/tool-engineering
git merge test/auditor --no-edit
```

---

**Recommendation:** Use the **single-branch linear history** (first section) and run each command block after review. Use the branch strategy only if you need separate PRs or branch-level rollback.
