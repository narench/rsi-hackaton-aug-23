---
marp: true
theme: default
paginate: true
backgroundColor: '#f8fafc'
color: '#17201c'
style: |
  section { font-family: Inter, Arial, sans-serif; padding: 54px 70px; }
  h1 { color: #12372a; font-size: 48px; }
  h2 { color: #166534; }
  strong { color: #166534; }
  blockquote { border-left: 8px solid #f59e0b; background: #fffbeb; padding: 16px 24px; }
  small { color: #64748b; }
  section.compact { font-size: 22px; }
  section.compact h1 { font-size: 40px; }
footer: 'DrugGraph — Self-Improving FDA Drug Intelligence'
---

# 1. Retrieval is the safety layer

## An AI answer is only as reliable as the evidence it retrieves

- FDA evidence is precise: **product, strength, route, dosage form, and regulatory status** all matter.
- Retrieving the wrong cohort can still produce a fluent, convincing answer.
- Generation cannot recover evidence that retrieval missed—or detect evidence that retrieval mixed incorrectly.

> **Better retrieval is more important than better wording.**

**Goal:** make retrieval observable, testable, and capable of improving from failure.

<!--
Talk track: Drug research is not just search. A plausible answer over the wrong product cohort is still wrong. We treat retrieval itself as the system that needs evaluation and improvement.
-->

---

# 2. FDA retrieval is a graph problem

| Source | Evidence |
|---|---|
| **Orange Book** | Products, applications, RLD/RS status, TE codes, patents |
| **Purple Book** | Reference biologics, biosimilars, interchangeable products |
| **openFDA** | Drugs@FDA, SPL labels, and NDC listings |

### The graph connects

**Drug names → products → applications → regulatory evidence → labels**

- Approximately **95K nodes** and **215K relationships**
- Provenance remains attached to every claim
- State substitution law remains a separate, versioned source

<!--
Talk track: These datasets answer different regulatory questions. A graph preserves their relationships and provenance instead of flattening everything into one misleading table.
-->

---

<!-- _class: compact -->

# 3. DrugGraph system design

![h:330 DrugGraph system design](skills/dynamic-query-neo4j/demo/docs/druggraph-system-design.svg)

- **Neo4j:** FDA evidence and provenance
- **LangGraph:** online repair and offline optimization
- **SQLite + YAML:** durable episodes, checkpoints, policy lineage, and rollback

<!--
Talk track: The online loop repairs the current request. The offline loop learns a durable policy improvement. Keeping those loops separate prevents an untested repair from becoming production behavior.
-->

---

<!-- _class: compact -->

# 4. Self-improving retrieval—not self-modifying code

```text
Detect failure → Repair request → Record episode
       ↓
Generate bounded policies → Run graph-backed evals
       ↓
Hard safety gates → Human approval → Promote → Replay
```

### Demonstrated policy evolution

| | Policy v1 | Promoted v2 |
|---|---:|---:|
| Evaluation score | **37.5%** | **100%** |
| Lipitor query attempts | **2** | **1** |
| Adalimumab capability | Blocked | Enabled after Purple Book gate |

**Promotion rule:** higher score + all hard checks + zero regressions + approval. Every mutation is allowlisted and shown as a YAML diff.

<!--
Talk track: This is bounded hill-climbing over retrieval policy, not model training. The optimizer cannot rewrite Python or invent arbitrary Cypher.
-->

---

<!-- _class: compact -->

# 5. What worked—and what comes next

## What worked

- Deterministic FDA invariants caught errors that an LLM judge might miss.
- Graph-backed evaluation made policy improvement measurable.
- Approval, versioning, reset, and rollback made the loop demoable and auditable.

## Next

- Add an **LLM judge** for usefulness and clarity—never as the sole promotion gate.
- Add an LLM proposer that emits only structured, allowlisted policy patches.
- Evaluate on frozen FDA graph snapshots and shadow production traffic.
- Expand RSI to label monitoring, biologic navigation, and sourced state-law rules.

> **DrugGraph turns retrieval failures into tested, durable improvements.**

<!--
Close: The contribution is not another chatbot. It is a controlled mechanism for making regulated-data retrieval improve without surrendering safety or auditability.
-->
