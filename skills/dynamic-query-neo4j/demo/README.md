# Purple Book + openFDA Neo4j demo

A hackathon-sized FDA drug graph. It automatically discovers the latest valid **Purple Book** monthly CSV, downloads the current **Orange Book** products/patents/exclusivities ZIP, and enriches a bounded biologics cohort from three openFDA datasets:

- **Drugs@FDA** — application products and submission history;
- **drug labeling** — SPL label versions and selected sections;
- **NDC Directory** — marketed product and package listings.

The MVP intentionally does not ingest FAERS. FAERS is much larger, its harmonized application identifiers are optional, and a report-level drug/reaction co-occurrence does not establish causality or incidence.

It supports three graph workflows:

1. Orange Book generic/TE and Purple Book biosimilar/interchangeable navigation;
2. federal substitution evidence checks (state rules require a separate versioned source);
3. current SPL label comparison and future change monitoring. Label nodes retain SPL IDs, versions, effective times, first/last observation times, warnings, indications, formulation/strength, routes, inactive ingredients, storage, and administration sections. Monitoring starts once more than one version of the same SPL set has been observed.

## Graph model

```text
(DatasetSnapshot)-[:CONTAINS]->(BiologicProduct)<-[:HAS_PRODUCT]-(Application)
(DatasetSnapshot)-[:CONTAINS]->(OrangeBookProduct)<-[:HAS_ORANGE_BOOK_PRODUCT]-(Application)
(OrangeBookProduct)-[:HAS_PATENT]->(Patent)
(OrangeBookProduct)-[:HAS_EXCLUSIVITY]->(Exclusivity)
(Application)-[:HAS_OPENFDA_PRODUCT]->(OpenFDAProduct)
(Application)-[:HAS_SUBMISSION]->(Submission)
(Application)-[:HAS_LABEL]->(Label)
(Application)-[:HAS_NDC_LISTING]->(NdcProduct)-[:HAS_PACKAGE]->(Package)
(Application)-[:REFERENCES {match_method, confidence}]->(Application)
(BiologicProduct|OrangeBookProduct|OpenFDAProduct|NdcProduct)-[:HAS_NAME]->(DrugName)
```

`Application.bla` is the stitch key: a Purple Book value such as `125057` is preserved and normalized to the openFDA form `BLA125057`. Product-level Purple Book ↔ Drugs@FDA edges require the same application **and** product number. `DrugName` is only a lexical discovery hub, not proof that two source records are the same product.

## Run

Requirements: Docker with Compose and internet access.

```bash
cd skills/dynamic-query-neo4j/demo
cp .env.example .env                 # optional: add a free openFDA API key
docker compose up -d neo4j
docker compose --profile load run --rm loader
docker compose up -d --build chatbot
```

Open the chatbot at <http://localhost:8501>. Neo4j Browser remains available at <http://localhost:7474>; sign in as `neo4j` using the password in `.env` and run examples from [`queries.cypher`](queries.cypher).

The full current Orange Book is loaded; the Purple Book/openFDA enrichment cohort is bounded to 20 BLAs matching `adalimumab`, `trastuzumab`, or `pembrolizumab`. Each openFDA endpoint is capped at 100 records per BLA. The loader reports sampled API results and caches raw openFDA responses under `data/`.

Customize before loading:

```bash
SEED_TERMS=rituximab,bevacizumab MAX_APPLICATIONS=12 \
  docker compose --profile load run --rm loader
```

If environment interpolation does not pick up inline values in your Compose version, put them in `.env` instead. `PURPLEBOOK_URL=latest` scans FDA's download page and chooses the newest valid CSV rather than guessing inconsistent filenames. Set an explicit URL only when you need a frozen snapshot.

Stop the database:

```bash
docker compose down                 # keep the graph volume
docker compose down -v              # delete this demo graph
```

## Provenance and join rules

- The latest Purple Book URL is discovered from FDA on every load. Its monthly file has a changes section followed by a complete snapshot; only the latter is ingested.
- The Orange Book stable current-data URL is refreshed on every load. Products join to NDA/ANDA applications; patents and exclusivities join through exact application and product numbers.
- Purple Book BLA values remain strings and are zero-padded to six digits before adding the `BLA` prefix.
- openFDA records join only at the exact application number. A missing openFDA result is expected for some CBER-regulated products.
- Purple Book `REFERENCES` edges are created only when a normalized reference proper/brand name has one unique 351(a) application candidate inside the loaded cohort. Ambiguous names are left unresolved.
- NDC listings attach at application level. An NDC listing does **not** by itself denote FDA approval, and the loader does not guess a Purple Book product match from names.
- Labels are retained by SPL document `id`; `set_id` groups versions. Queries must choose the newest effective version rather than assuming one label per BLA. Missing sections are unavailable evidence, not proof of equality.
- Orange Book A/AB codes and Purple Book interchangeability are federal evidence. This demo does not load state pharmacy law and must not claim state-level substitution eligibility.
- Long label sections are capped at 20,000 characters for this small graph. Raw cached API responses preserve the complete response.

## RSI demonstration

The chatbot includes a **Run live RSI demo** button. It performs a real end-to-end loop:

1. resets to query policy v1;
2. runs an ingredient-only Lipitor query;
3. detects mixed strength/form cohorts and repairs the current query;
4. persists the episode and repair trace in SQLite;
5. generates three bounded policy descendants;
6. evaluates them against executable graph benchmarks;
7. pauses at a LangGraph approval interrupt;
8. promotes the winning v2 policy atomically;
9. replays the same prompt in one query attempt instead of two.

The sidebar exposes three RSI parameters:

- **Online repair attempts (2–5):** maximum query/validate attempts for one request.
- **Policy candidates per round (1–3):** bounded policy descendants evaluated in parallel conceptually (evaluated deterministically in this MVP).
- **Optimization rounds (1–3):** maximum recursive promotion rounds; execution stops early when no candidate beats the active policy.

The results panel also states what RSI runs on: LangGraph 1.2.11 orchestration, a bounded deterministic policy mutator, three graph-backed substitution evals, the current Neo4j Orange Book snapshot, and SQLite episode/checkpoint persistence. **Reset demo to policy v1** clears generated policy versions, episodes, evaluations, and checkpoints.

The same flow is available from the CLI:

```bash
source .venv/bin/activate
python -m rsi.cli demo
```

Useful commands:

```bash
python -m rsi.cli reset
python -m rsi.cli ask "Find substitutes for Lipitor 10 mg oral tablets"
python -m rsi.cli improve              # pause and print a thread ID
python -m rsi.cli approve THREAD_ID    # resume checkpoint and promote
python -m rsi.cli reject THREAD_ID     # resume checkpoint without promotion
python -m rsi.cli improve --approve    # one-command demo shortcut
python -m rsi.cli rollback 1           # atomically restore v1
pytest -q
```

The implemented RSI slice currently covers Orange Book substitution query generation. Existing biologic navigation and label comparison remain deterministic chatbot paths. Generated queries are restricted to one parameterized read-only statement with a 15-second timeout; the Compose demo still uses the Neo4j admin credential, so deploy with a dedicated read-only account before production. See [`RSI-DESIGN.md`](RSI-DESIGN.md) for the broader architecture and boundaries.

## Data and API limits

The no-key openFDA quota is currently much smaller than the keyed daily quota. Get a key from the official [authentication page](https://open.fda.gov/apis/authentication/) for repeated experiments. API calls have a maximum page size of 1,000; this demo deliberately loads only the first configured page and records the source URL on imported nodes.

Do not use this graph alone for clinical decisions. Label content, application history, and NDC listings answer different regulatory questions and should not be conflated.

Official sources:

- [Purple Book downloads](https://purplebooksearch.fda.gov/index.cfm?event=downloads)
- [Orange Book data files](https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files)
- [Drugs@FDA API](https://open.fda.gov/apis/drug/drugsfda/)
- [Drug labeling API](https://open.fda.gov/apis/drug/label/)
- [NDC API and listing caveats](https://open.fda.gov/apis/drug/ndc/)
