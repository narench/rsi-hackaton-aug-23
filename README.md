# FDA Neo4j Dynamic Query Optimization

A hackathon-ready FDA research graph and durable recursive self-improvement (RSI) demo built with Neo4j, LangGraph, and Streamlit.

The project focuses on one problem: safely improving dynamic graph-query policies from observed failures without allowing an optimizer to rewrite arbitrary application code.

## What the demo shows

1. **Policy v1 fails safely** — the baseline uses an incomplete Orange Book cohort policy and blocks adalimumab interchangeability because the biologic capability has not been approved.
2. **Online repair** — a Lipitor substitution query detects mixed strength and dosage-form cohorts, adds the missing constraints, and answers on its second attempt.
3. **Offline optimization** — bounded policy descendants compete against executable Neo4j benchmarks.
4. **Deterministic promotion gates** — candidates must improve the baseline score, pass all hard checks, and introduce no regressions.
5. **Visible policy promotion** — the UI displays runtime configuration, round results, scores, mutation operations, and the complete v1-to-v2 YAML diff.
6. **Replay under v2** — Lipitor succeeds in one attempt and the validated Purple Book capability allows the adalimumab query.
7. **Reset and rollback** — the demo can return to policy v1 from the UI or CLI.

This is policy optimization, not model-weight training. The optimizer searches a small allowlisted mutation space and persists only evaluated, approved winners.

## Data sources

The graph loader combines:

- FDA Orange Book products, patents, exclusivities, RLD/RS fields, and complete TE codes
- FDA Purple Book reference, biosimilar, and interchangeable application relationships
- openFDA Drugs@FDA enrichment
- SPL labels
- NDC product listings

State substitution law is intentionally not inferred. Purple Book and Orange Book evidence is presented separately from state-specific dispensing rules.

## Quick start

Requirements:

- Docker with Compose (OrbStack works)
- Internet access for the initial FDA data load

```bash
git clone https://github.com/narench/rsi-hackaton-aug-23.git
cd rsi-hackaton-aug-23/skills/dynamic-query-neo4j/demo
cp .env.example .env

docker compose up -d neo4j
docker compose --profile load run --rm loader
docker compose up -d --build chatbot
```

Open:

- Streamlit demo: <http://localhost:8501>
- Neo4j Browser: <http://localhost:7474>

The default demo credentials are documented in `.env.example`. Change them outside a local hackathon environment.

## Judge flow

1. Click **Reset demo to policy v1**.
2. Ask **Which adalimumab products are interchangeable?**
   - v1 blocks the query with `MISSING_VALIDATED_BIOLOGIC_POLICY`.
3. Click **Run live RSI demo**.
   - The UI shows the failed cohort invariant, candidate evaluations, promotion decision, and full policy diff.
4. Ask the adalimumab question again.
   - v2 returns the loaded Purple Book family.
5. Reset to v1 and repeat as needed.

The sidebar controls:

- Maximum online query-repair attempts
- Policy candidates per optimization round
- Maximum optimization rounds

## RSI implementation

```text
User request
    |
    v
LangGraph online loop
  resolve -> plan -> execute -> validate -> repair
    |
    v
SQLite episode and attempt traces
    |
    v
Offline policy optimizer
  collect -> propose -> evaluate -> select -> approve -> promote
    |
    +--> Neo4j graph-backed benchmark gates
    +--> versioned YAML policies
    +--> atomic active-policy pointer
```

Safety boundaries:

- Generated Cypher must be a single read-only statement.
- Parameters are separated from query text.
- Forbidden write/admin clauses and comments are rejected.
- Queries have a 15-second timeout and 100-row boundary.
- Policy mutations use an allowlisted structured operation vocabulary.
- Promotion requires deterministic hard gates and approval.
- Rollback atomically restores an existing policy version.

See [`skills/dynamic-query-neo4j/demo/RSI-DESIGN.md`](skills/dynamic-query-neo4j/demo/RSI-DESIGN.md) for implementation details and production-hardening boundaries.

## CLI

```bash
cd skills/dynamic-query-neo4j/demo
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-rsi.txt

python -m rsi.cli reset
python -m rsi.cli demo
```

Manual approval and rollback:

```bash
python -m rsi.cli improve
python -m rsi.cli approve THREAD_ID
python -m rsi.cli reject THREAD_ID
python -m rsi.cli rollback 1
```

## Tests

```bash
cd skills/dynamic-query-neo4j/demo
source .venv/bin/activate
pytest -q
```

## Repository layout

```text
skills/dynamic-query-neo4j/
├── SKILL.md                       # Dynamic Neo4j query workflow
├── CYPHER-PATTERNS.md             # FDA-oriented query patterns
├── dynamic-query-neo4j.eval.yaml  # Skill evaluation tasks
└── demo/
    ├── chatbot.py                 # Streamlit research console
    ├── load_graph.py              # FDA graph ingestion
    ├── compose.yaml               # Neo4j, loader, and chatbot services
    ├── RSI-DESIGN.md              # RSI architecture and boundaries
    ├── rsi/                       # Online/offline optimizer implementation
    └── tests/                     # Safety and policy tests
```

## Current scope

The durable optimizer currently specializes in Orange Book substitution policy and a Purple Book interchangeability capability gate. Biologic navigation and label comparison are available in the chatbot, but broader autonomous optimization for those workflows remains future work.

## Future work

- **LLM-as-judge:** Add qualitative scoring for usefulness and clarity while keeping deterministic checks as mandatory promotion gates. An LLM score must never promote a policy by itself.
- **LLM policy proposer:** Generate structured, allowlisted policy patches instead of arbitrary code or unrestricted Cypher.
- **Broader RSI coverage:** Optimize biologic navigation, label comparison, and safety-monitoring workflows.
- **Frozen evaluation graph:** Evaluate candidates against reproducible FDA snapshots in isolated Neo4j containers.
- **State substitution rules:** Load sourced, effective-dated state laws separately from federal FDA evidence.
- **Adversarial evaluation:** Test prompt injection, unsafe Cypher, fabricated evidence, and incorrect substitution claims.
- **Shadow deployment:** Compare candidate policies against real traffic before promotion, with canary rollout and automatic rollback.
- **Better observability:** Add live optimizer logs, cost and latency metrics, lineage dashboards, and failure clustering.
- **Production security:** Use dedicated read-only Neo4j credentials, authentication, rate limits, and complete audit trails.
