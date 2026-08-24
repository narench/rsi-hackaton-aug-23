# Dynamic Neo4j Query Optimization Skill

This repository contains one skill: [`dynamic-query-neo4j`](dynamic-query-neo4j/).

It guides an agent through safe, schema-aware Neo4j research:

1. inspect the live schema and provenance;
2. resolve entities before traversal;
3. generate parameterized, read-only Cypher;
4. validate result grain and domain invariants;
5. repair empty, ambiguous, or over-broad results;
6. report evidence and limitations without inventing graph structure.

The included FDA demo extends the skill with a durable LangGraph RSI loop that records failures, evaluates bounded policy mutations, promotes an approved winner, and supports reset and rollback.

## Install the skill

```bash
npx skills add narench/rsi-hackaton-aug-23 --skill dynamic-query-neo4j
```

## Run its eval

```bash
caliper run skills/dynamic-query-neo4j/dynamic-query-neo4j.eval.yaml --k 3 --baseline
```

## Run the live demo

```bash
cd skills/dynamic-query-neo4j/demo
cp .env.example .env
docker compose up -d neo4j
docker compose --profile load run --rm loader
docker compose up -d --build chatbot
```

Open <http://localhost:8501>.

See the repository [`README.md`](../README.md) and [`demo/README.md`](dynamic-query-neo4j/demo/README.md) for the full workflow.
