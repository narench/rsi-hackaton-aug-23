# RSI design: FDA graph query agent

## Implementation status

The working MVP implements the substitution vertical slice: online LangGraph repair, SQLite episode/attempt traces, three bounded policy descendants, graph-backed benchmark evaluation, approval interrupt, atomic promotion, rollback, and Streamlit replay. Biologic and label workflows remain deterministic chatbot paths. Git worktrees, a provider-backed candidate model, a frozen Testcontainers fixture, and a dedicated Neo4j read-only account remain production-hardening work.

## Goal

Improve query planning, validation, and repair over time without allowing an untested request to rewrite production behavior. Online execution self-corrects a single request. Offline RSI turns repeated successful repairs into versioned policy or query-template candidates, evaluates them against a frozen graph, and promotes only non-regressing winners.

## Non-goals

- Autonomous changes to FDA source data, Neo4j constraints, importers, identity edges, or state-law content.
- Treating model agreement as proof of regulatory or graph correctness.
- Promoting request-specific parameter values as general policy.
- Allowing generated write Cypher against the FDA graph.

## Runtime architecture

```text
Streamlit chatbot
  |
  v
LangGraph online graph -----------------> Neo4j FDA graph
  |
  +--> SQLite ResearchRun / QueryAttempt traces
              |
Background RSI worker <------------------+
  |
  +--> candidate policy versions
  +--> graph-backed benchmark evaluation
  +--> deterministic assertions
  +--> approval interrupt
  +--> atomic promotion / rollback
```

Neo4j stores FDA evidence. SQLite stores RSI episodes, evaluation lineage, and LangGraph checkpoints so the optimizer does not write metadata into the FDA graph. Versioned YAML and an atomic active-policy pointer store promoted behavior.

## Online LangGraph

```text
START
  -> classify_intent
  -> resolve_identity
  -> plan_query
  -> enforce_read_only
  -> execute
  -> validate
       pass -> render -> persist_episode -> END
       repairable -> repair -> enforce_read_only (maximum 3 attempts)
       ambiguous -> clarify interrupt -> resolve_identity
       terminal -> safe_failure -> persist_episode -> END
```

### Online state

```python
class QueryState(BaseModel):
    run_id: str
    thread_id: str
    question: str
    conversation: list[Message]
    policy_version: int
    attempt: int = 0
    intent: str | None = None
    identity: ResolvedIdentity | None = None
    plan: QueryPlan | None = None
    rows: list[dict] = []
    validation: ValidationResult | None = None
    repairs: list[RepairRecord] = []
    answer: AnswerEnvelope | None = None
```

### Hard online controls

- Read-only Neo4j user.
- Reject `CREATE`, `MERGE`, `SET`, `DELETE`, `REMOVE`, `DROP`, `LOAD CSV`, `CALL { ... IN TRANSACTIONS }`, and unapproved procedures.
- Parameterized user values only.
- Maximum 100 returned rows, 15-second query timeout, and three attempts.
- Validators, not the LLM, decide whether execution passes.

## Offline RSI LangGraph

```text
START
  -> collect_failed_or_repaired_episodes
  -> cluster_by_failure
  -> select_cluster
  -> propose_candidates (three branches)
  -> validate_patch_schema
  -> create_candidate_worktrees
  -> run_static_checks
  -> run_trigger_replay
  -> run_regression_suite
  -> run_held_out_suite
  -> score_candidates
       no eligible candidate -> reject -> END
       eligible winner -> approval interrupt
          rejected -> archive -> END
          approved -> promote -> reload_active_policy -> END
```

The next optimization starts from the last promoted version, making the process recursive:

```text
v1 -> v2 -> v3 -> v4
```

## Mutation hierarchy

Promote the smallest generalizable change:

| Failure | Mutation |
|---|---|
| Wrong request parameter | Current query only, never promoted |
| Inefficient or incorrect traversal | Parameterized Cypher template |
| Missing identity requirement | Query policy |
| Repeated repair strategy | Query policy |
| New known failure | Benchmark case |
| Source column renamed | Schema mapping candidate, human approval |
| Missing data source | Coverage request, no automatic patch |

## Versioned artifacts

```text
rsi/
  active-policy.json
  policies/
    query-policy.v1.yaml
  queries/
    substitution.cypher
    biologic-family.cypher
    label-comparison.cypher
  benchmarks/
    substitution.jsonl
    biologics.jsonl
    labels.jsonl
    schema-drift.jsonl
    held-out.jsonl
  checkpoints/
    rsi.sqlite
  reports/
```

`active-policy.json` is updated atomically only after promotion.

## Query policy

```yaml
version: 1
parent: null

intents:
  substitution:
    source: orange_book
    required_identity:
      - ingredient
      - strength
      - dosage_form_route
    cohort_fields:
      - ingredient
      - strength
      - dosage_form_route
    validators:
      - one_ingredient
      - one_strength
      - one_form_route
      - preserve_te_subcode
      - no_state_claim_without_rule
    repairs:
      MISSING_STRENGTH: ask_clarification
      MIXED_STRENGTH: ask_clarification
      EMPTY_RESULT: relax_filters_incrementally

  biologic_family:
    source: purple_book
    validators:
      - reference_edge_has_provenance
      - biosimilar_not_assumed_interchangeable

  label_comparison:
    source: openfda_label
    validators:
      - newest_version_per_set
      - report_spl_identity
      - missing_section_is_unknown
```

Candidate generation is restricted to a JSON/Pydantic patch vocabulary. Arbitrary code patches are not eligible for automatic promotion.

## Structured execution artifacts

### Query plan

```python
class QueryPlan(BaseModel):
    intent: str
    template_id: str
    cypher: str
    parameters: dict[str, object]
    expected_grain: list[str]
    required_evidence: list[str]
```

### Validation

```python
class ValidationResult(BaseModel):
    passed: bool
    failures: list[str]
    metrics: dict[str, float | int | str]
    repairable: bool
```

### Answer envelope

```python
class AnswerEnvelope(BaseModel):
    intent: str
    resolved_entities: dict
    result_summary: dict
    claims: list[Claim]
    limitations: list[str]
    evidence_ids: list[str]
    text: str
```

The rendered prose is never the only evaluation artifact.

## Deterministic validators

Initial validator registry:

- `cypher_read_only`
- `parameters_only`
- `one_resolved_identity`
- `one_ingredient`
- `one_strength`
- `one_form_route`
- `preserve_te_subcode`
- `reported_equivalents_have_a_prefix`
- `reference_edge_has_provenance`
- `biosimilar_not_assumed_interchangeable`
- `no_state_claim_without_rule`
- `newest_version_per_set`
- `report_spl_identity`
- `missing_section_is_unknown`
- `row_count_not_limit_artifact`
- `required_properties_coverage`

## Episode and lineage storage

SQLite stores three bounded tables:

```text
episodes
  run_id, question, intent, policy_version, status, attempts,
  failures_json, repairs_json, attempts_json, answer_json, created_at

evaluations
  id, parent_version, candidate_id, candidate_policy_json,
  evaluation_json, created_at

promotions
  version, parent_version, candidate_id, score, created_at
```

Each attempt trace includes the parameterized Cypher, parameters, row count, and deterministic validation result. The FDA graph is never mutated by the RSI loop.

## Candidate generation

The model receives:

- current policy and relevant query template;
- one clustered failure summary;
- representative failed and repaired episodes;
- observed graph schema;
- allowed patch operations;
- required regression-case schema.

It returns three `PolicyPatch` candidates. No candidate sees the held-out suite.

The model provider is behind an adapter:

```python
class CandidateModel(Protocol):
    def propose(self, request: PatchRequest) -> list[PolicyPatch]: ...
```

This leaves Gemini, OpenAI-compatible APIs, or another provider selectable through environment configuration without changing the RSI graph.

## Evaluation

### Infrastructure

- `pytest` for executable assertions.
- `pytest-json-report` for candidate scoring artifacts.
- `testcontainers[neo4j]` for a frozen Neo4j 5.26 fixture.
- Existing Caliper YAML for skill activation and high-level behavior.
- Optional LLM judge for clarity only, never hard correctness.

### Frozen fixture

Include:

- Lipitor and atorvastatin across multiple strengths.
- A, AB, AB1, AB2, B, and missing TE codes.
- Humira reference plus biosimilar and interchangeable applications.
- Multiple SPL sets and historical versions.
- Missing state-rule data.
- Purple Book old/new header fixtures.

### Score

```text
25 identity/cohort correctness
25 graph-result correctness
20 regulatory safety
15 Cypher validity and parameterization
10 regression performance
 5 latency/query cost
```

### Hard rejection

- Any write Cypher.
- Mixed pharmaceutical-equivalence cohort.
- Biosimilar represented as interchangeable without evidence.
- State-law conclusion without a current sourced rule.
- Arbitrary SPL-set comparison represented as equivalent.
- Any regression in a hard-safety case.
- Candidate saw or modified held-out cases.

### Promotion

```python
eligible = (
    fixes_trigger
    and hard_failures == 0
    and regressions == 0
    and score > parent_score
)
```

## Git lifecycle

1. Create one worktree per candidate under a temporary directory.
2. Apply structured patch and generated managed documentation blocks.
3. Run static checks and test suites.
4. Archive reports for every candidate.
5. Interrupt for approval with diff and before/after scores.
6. On approval, merge the winning policy commit and atomically update `active-policy.json`.
7. Preserve parent version and checkpoint for rollback.

## Observability

Each UI response shows:

- policy version;
- attempt count;
- selected source datasets;
- identity grain;
- validation status;
- expandable Cypher and parameters.

The RSI panel shows:

- triggering failure;
- candidate diff;
- trigger replay result;
- regression and held-out scores;
- promotion or rejection status.

## Judge demonstration

1. Ask: `Can generic Lipitor be substituted?`
2. Baseline policy creates a mixed-strength cohort.
3. Deterministic critic reports four strengths and rejects the conclusion.
4. Online loop asks for strength and succeeds for 10 mg.
5. Offline optimizer proposes making strength mandatory.
6. Three candidate branches run the frozen benchmark.
7. Winning candidate improves the score without regressions.
8. Judge approves promotion from v1 to v2.
9. Repeat the original question; v2 asks for strength on the first attempt.

This demonstrates immediate self-correction, durable policy mutation, evaluation, lineage, and rollback.

## Delivery phases

### Phase 1: online loop

- Typed state and answer envelope.
- Read-only executor.
- Three core intents.
- Deterministic validators and repair routing.
- Episode persistence.

### Phase 2: reproducible evaluation

- Frozen testcontainer fixture.
- Anchor, adversarial, metamorphic, and held-out suites.
- JSON candidate reports.

### Phase 3: offline RSI

- Candidate schema and model adapter.
- Worktree evaluation fan-out.
- LangGraph approval interrupt.
- Promotion, reload, and rollback.

### Phase 4: demo integration

- Streamlit trace panel.
- One-click real failure replay.
- Candidate diff and before/after score display.
- Policy lineage view.
