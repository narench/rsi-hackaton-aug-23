---
name: dynamic-query-neo4j
description: Research drugs in Neo4j using openFDA, Orange Book, and Purple Book data. Use for generic/biosimilar navigation, therapeutic-equivalence or interchangeability checks, product substitution evidence, label comparisons/change monitoring, drug identity, labels, FAERS, recalls, NDC products, or Drugs@FDA applications. Always inspect the live graph schema and provenance before querying.
---

# Research openFDA drug data in Neo4j

Use the graph as an index over openFDA records, not as a clinical oracle. The graph schema and the source snapshot are both dynamic: importers rename labels, flatten arrays differently, and update datasets on different schedules. Never invent a graph model from the openFDA JSON model.

## 1. Frame the research question

Identify the unit of analysis and choose the source before querying:

| Question | Preferred openFDA source | Unit |
|---|---|---|
| Current FDA labeling, indications, warnings, contraindications | Drug labeling | SPL label document/set |
| Reported adverse events or medication errors | Drug adverse events (FAERS) | Safety report, not patient or event |
| Marketed product/package attributes | NDC Directory | Product/package listing |
| Approval history, sponsors, submissions | Drugs@FDA | Application/product/submission |
| Licensed biologics, biosimilars, reference products | Purple Book | BLA/product |
| Approved NDA/ANDA products, therapeutic equivalence, patents | Orange Book | Application/product |
| Recall actions and status | Drug enforcement | Enforcement report |

Ask for a date range, geography, comparison drug or background population when those change the answer. If the question asks whether a drug *causes* an event, or which drug is clinically safer, restate it as a descriptive or signal-detection question; openFDA alone cannot answer causality, incidence, prevalence, or comparative clinical safety.

## 2. Connect and establish provenance

Find a connection in this order, stopping at the first success:

1. `NEO4J_URI`, `NEO4J_USERNAME`, and `NEO4J_PASSWORD` in the environment or repository `.env`.
2. A configured Neo4j MCP tool that can run Cypher.
3. A running `neo4j` container (`docker ps`; Bolt is commonly exposed on `7687`).

If none works, report all three checks and ask for connection details. Verify with `RETURN 1`.

Before analysis, find import metadata in the graph or repository: source endpoint/file, retrieval date, covered date range, importer version, and whether the load is complete or sampled. Never describe a result as “current openFDA data” unless the snapshot date is known. Different endpoints update on different schedules; FAERS is updated quarterly and can lag by at least several months, while enforcement data is updated separately.

## 3. Recon the live schema

Run these every session:

```cypher
CALL db.labels();
CALL db.relationshipTypes();
CALL db.propertyKeys();
CALL apoc.meta.schema();
```

If APOC is unavailable, inspect only the relevant labels and relationships:

```cypher
MATCH (n) RETURN labels(n) AS labels, count(*) AS rows ORDER BY rows DESC;
MATCH ()-[r]->() RETURN type(r) AS relationship, count(*) AS rows ORDER BY rows DESC;
MATCH (n:CandidateLabel) RETURN n LIMIT 3;
MATCH (n:CandidateLabel)-[r]-(m)
RETURN type(r), labels(m), count(*) AS rows ORDER BY rows DESC;
```

Confirm how nested openFDA arrays were represented. In particular, inspect real samples for:

- drug identity (`brand_name`, `generic_name`, `substance_name`, `product_ndc`, `package_ndc`, `application_number`, `spl_id`, `spl_set_id`, or importer equivalents);
- FAERS report identity/version, drug role, reactions, outcomes, dates, and indications;
- label section text, application/product mapping, and SPL document/version/set identity;
- Orange Book application/product number, ingredient, dosage form/route, strength, RLD/RS flags, TE code, applicant, and approval date;
- Purple Book BLA/product number, license type (`351(a)`, biosimilar, or interchangeable), reference-product edge, applicant, and approval date;
- optional state substitution rule, jurisdiction, product class, effective dates, authority URL, and snapshot;
- enforcement report identity, status, classification, dates, and product description;
- import or snapshot metadata.

**Do not write the final query until every label, property, relationship, and direction it uses has been observed in the live graph.** All names in [`CYPHER-PATTERNS.md`](CYPHER-PATTERNS.md) are placeholders.

## 4. Resolve the drug before counting

Drug names are not stable join keys. A user term may name an ingredient, brand, multi-ingredient product, dosage form, or NDC. Resolve it explicitly:

1. Search exact normalized identifiers first (NDC, application number, SPL set ID).
2. Otherwise search the observed brand, generic, and substance fields separately.
3. Return candidate names plus identifiers, ingredients, route/dosage form, and manufacturer/sponsor.
4. Ask the user to choose when matches span different ingredients or combination products.
5. Keep the selected identifier set as parameters for later queries.

Use `.exact` fields in the live openFDA API when appropriate; in Neo4j use normalized equality for confirmed identities. `CONTAINS` is useful only for discovery and can merge unrelated products. Do not join endpoints solely on a display name. Prefer durable identifiers, and disclose when a cross-dataset join is approximate or many records lack harmonized `openfda` fields.

## 5. Run the supported workflow

### A. Generic/biosimilar relationship navigator

First classify the resolved item as a small-molecule Orange Book product, a Purple Book biologic, or an ambiguous match.

For a **small molecule**:

1. Form the pharmaceutically equivalent candidate cohort from the confirmed active ingredient(s), strength, dosage form, and route—not ingredient alone.
2. Identify the reference listed drug/reference standard using observed RLD/RS fields.
3. Return each application/product number, trade name, applicant/manufacturer, approval date, RLD/RS flags, and full Orange Book TE code.
4. Keep different strengths, routes, dosage forms, and combination products separate.

For a **biologic**:

1. Identify the `351(a)` reference BLA/product.
2. Follow only an imported Purple Book reference relationship with provenance. Do not manufacture reference edges from fuzzy names.
3. Return BLA/product number, brand/proper name, applicant, approval date, and exact Purple Book designation: reference, `351(k) Biosimilar`, or `351(k) Interchangeable`.

Always show unresolved candidates and missing approval/manufacturer fields rather than silently dropping them. See the navigator recipes in [`CYPHER-PATTERNS.md`](CYPHER-PATTERNS.md).

### B. Substitution eligibility checker

Report **federal evidence**, not a prescribing or dispensing decision:

- For small molecules, report the complete Orange Book TE code in the pharmaceutically equivalent cohort. Codes beginning with `A` indicate products FDA considers therapeutically equivalent under Orange Book definitions; preserve subdivisions such as `AB1`/`AB2`. A `B` code or missing TE code is not affirmative TE evidence.
- For biologics, distinguish the reference product, biosimilar, and FDA-designated interchangeable biosimilar. Biosimilar status alone is not interchangeable status.
- Never turn shared ingredient, RLD, RS, `351(k)`, or an NDC listing into an affirmative substitution result by itself.

State pharmacy law is a separate evidence layer. If versioned `StateRule` data with an authority URL and effective dates exists, retrieve the latest applicable rule and report its snapshot date. Otherwise say state-specific eligibility is **not evaluated** and ask for the state; do not infer a rule from federal designation. Even an FDA TE/interchangeability designation does not by itself answer patient-specific, prescriber, payer, inventory, or state-law requirements.

### C. Label difference monitor

Resolve both products to confirmed application/product identifiers, then retrieve the correct SPL label sets. A BLA/application can have multiple label sets, packagers, and versions; compare like-for-like label sets or explain why the comparison is cross-set.

For each `set_id`, select the newest version by `effective_time` and numeric `version`. Compare these sections independently:

- boxed warnings, warnings/cautions, and contraindications;
- indications and usage;
- dosage forms/strengths, formulation/description, and inactive ingredients/excipients;
- route and dosage/administration instructions;
- how supplied and storage/handling.

Missing text means “not available in this graph,” not “no difference.” Return effective time, version, SPL IDs, and source URLs with each side. Use Cypher to retrieve bounded text and metadata; perform a normalized section-by-section text diff outside Cypher when useful, preserving the original text for evidence.

For monitoring, retain historical `Label` nodes rather than overwriting them. Compare the newest two versions within the same `set_id`; alert only when a new SPL document/version appears and one or more normalized sections change. Record section names and old/new IDs in the alert, and make repeated runs idempotent. If the graph stores only current labels, explain that historical change detection is impossible until snapshots are retained.

## 6. Query at the correct grain

- Parameterize all user values; never concatenate them into Cypher.
- Use `LIMIT 25` while exploring and aggregate before returning large result sets.
- Count `DISTINCT` source records, not exploded drug/reaction/ingredient paths.
- For FAERS, deduplicate follow-up versions according to the imported report/version fields. State the rule used; do not silently count every version as a new case.
- Restrict by drug role (for example, primary/secondary suspect) when the question requires it, and report the chosen role set.
- A FAERS report can contain several drugs and several reactions. A drug–reaction co-occurrence is report-level only; the report does not link a particular reaction to a particular drug.
- Define the denominator and time window for every percentage or signal metric. Raw report counts are influenced by exposure, reporting behavior, publicity, time on market, duplicates, and missing data.
- Keep label text, spontaneous reports, approvals, NDC listings, and recalls as separate evidence types. A term in a warning is not proof of a FAERS signal; a recall is not necessarily a safety recall.
- Read-only by default. Before any `CREATE`, `MERGE`, `SET`, or `DELETE`, show the exact statement and a read-only count of affected records and get explicit confirmation.

Reusable identity, FAERS, label, recall, trend, and coverage patterns are in [`CYPHER-PATTERNS.md`](CYPHER-PATTERNS.md). For a runnable hackathon graph that automatically loads the latest Purple Book, current Orange Book, and bounded openFDA enrichment, use [`demo/`](demo/).

## 7. Validate the result

Treat zero rows as a debugging symptom until proven otherwise. Remove clauses one at a time: date/role filters, relationship, identifier, then label. Check array flattening, relationship direction, date type, identifier punctuation, null coverage, and snapshot date. Compare counts at three levels where relevant: source records, distinct reports/documents, and exploded paths.

For any aggregate, run coverage checks and inspect sample source records. Flag suspicious totals equal to an exploratory `LIMIT`, abrupt date gaps, duplicate versions, and sparse identifier mapping. If possible, spot-check a small count against the corresponding openFDA endpoint using the same filters; label this as validation rather than silently mixing live API and graph snapshots.

## 8. Report responsibly

Give the answer, then:

1. **Scope:** dataset, graph snapshot/retrieval date, covered dates, drug identity/identifiers, geography, role filters, and deduplication rule.
2. **Result:** record counts and clearly named units (for example, “distinct FAERS safety reports”).
3. **Cypher:** the exact query and parameters.
4. **Coverage:** missing identifiers, sparse joins, excluded records, and source/version coverage.
5. **Interpretation:** distinguish label evidence, Orange Book TE evidence, Purple Book biosimilar/interchangeable designation, state law, approvals, recalls, and reported associations.

For navigator/substitution results, name the pharmaceutical-equivalence cohort fields and return the complete TE/license designation. State whether state law was evaluated and identify the rule date/source when it was. For label comparisons, identify both SPL documents, effective dates, compared sections, and any sections missing from either side.

For FAERS findings, always say that reports are unverified spontaneous reports, do not establish causation, cannot estimate incidence or risk without a valid exposure denominator, and should not be the sole basis for clinical decisions. Use “reported with” or “co-reported,” never “caused by.” Encourage consultation with an appropriate clinician or FDA source for medical decisions.

Official source documentation:

- Drug APIs: <https://open.fda.gov/apis/drug/>
- Orange Book: <https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files>
- Purple Book: <https://purplebooksearch.fda.gov/>
- FAERS/openFDA adverse events: <https://open.fda.gov/apis/drug/event/>
- Query syntax and paging: <https://open.fda.gov/apis/query-syntax/> and <https://open.fda.gov/apis/paging/>
