# Cypher patterns for openFDA drug research

Every label, relationship, direction, and property below is a **placeholder** until schema recon confirms it. The runnable demo in [`demo/`](demo/) intentionally uses the shown Purple Book/openFDA model, but a user's imported graph may not.

## Dataset provenance and coverage

Run this before trusting an aggregate:

```cypher
MATCH (n) RETURN labels(n) AS labels, count(*) AS rows ORDER BY rows DESC;
MATCH ()-[r]->() RETURN type(r) AS relationship, count(*) AS rows ORDER BY rows DESC;
MATCH (s:DatasetSnapshot)
RETURN s.dataset, s.source_url, s.retrieved_at, s.covered_from, s.covered_through,
       s.complete_source_snapshot, s.sample_limit;
```

Check identifier coverage explicitly:

```cypher
MATCH (r:SafetyReport)-[:HAS_DRUG]->(d:ReportDrug)
RETURN count(DISTINCT r) AS reports,
       count(DISTINCT CASE WHEN d.application_number IS NOT NULL THEN r END) AS mapped_reports,
       round(1000.0 * count(DISTINCT CASE WHEN d.application_number IS NOT NULL THEN r END)
             / count(DISTINCT r)) / 10 AS mapped_percent;
```

## Resolve a drug identity

Discovery may use text matching, but the final analysis should use confirmed identifiers:

```cypher
MATCH (entity)-[r:HAS_NAME]->(name:DrugName)
WHERE name.normalized CONTAINS toLower($term)
RETURN labels(entity) AS entity_type, entity, r.kind, r.source, name.display
LIMIT 25;
```

For Purple Book/openFDA application joins, preserve the BLA as text and use the normalized prefixed form (for example, `BLA125057`):

```cypher
MATCH (a:Application {bla: $bla})
OPTIONAL MATCH (a)-[:HAS_PRODUCT]->(p:BiologicProduct)
OPTIONAL MATCH (a)-[:HAS_LABEL]->(l:Label)
OPTIONAL MATCH (a)-[:HAS_NDC_LISTING]->(n:NdcProduct)
RETURN a, collect(DISTINCT p)[..25] AS products,
       count(DISTINCT l) AS labels, count(DISTINCT n) AS ndc_listings;
```

Do not infer a product-level match from a shared display name. An NDC listing is not itself proof of approval.

## Generic navigator: Orange Book pharmaceutical-equivalence cohort

Resolve `$product_id` first. Cohort on ingredient, strength, dosage form, and route; ingredient alone is unsafe:

```cypher
MATCH (selected:OrangeBookProduct {id: $product_id})
MATCH (candidate:OrangeBookProduct)
WHERE candidate.ingredient = selected.ingredient
  AND candidate.strength = selected.strength
  AND candidate.dosage_form_route = selected.dosage_form_route
MATCH (application:Application)-[:HAS_ORANGE_BOOK_PRODUCT]->(candidate)
RETURN application.application_number, candidate.product_number,
       candidate.trade_name, candidate.ingredient, candidate.strength,
       candidate.dosage_form_route, candidate.applicant_full_name,
       candidate.approval_date, candidate.rld, candidate.rs,
       candidate.te_code,
       CASE
         WHEN candidate.te_code STARTS WITH 'A' THEN 'FDA TE evidence present'
         WHEN candidate.te_code IS NULL THEN 'No TE code in graph'
         ELSE 'Not affirmative A-rated TE evidence'
       END AS federal_te_evidence
ORDER BY candidate.rld DESC, candidate.trade_name;
```

Preserve complete codes such as `AB1` and `AB2`; do not flatten them to `AB`. RLD/RS identifies a reference role but is not by itself an affirmative substitution result.

## Biosimilar navigator: Purple Book reference family

Resolve `$bla` first, then find the reference application and its loaded follow-ons:

```cypher
MATCH (selected:Application {bla: $bla})
OPTIONAL MATCH (selected)-[:REFERENCES]->(parent:Application)
WITH coalesce(parent, selected) AS reference
CALL (reference) {
  RETURN reference AS member
  UNION
  MATCH (follow_on:Application)-[:REFERENCES]->(reference)
  RETURN follow_on AS member
}
MATCH (member)-[:HAS_PRODUCT]->(product:BiologicProduct)
RETURN member.bla, member.applicant, member.bla_type AS purple_book_status,
       collect(DISTINCT product.proprietary_name) AS brands,
       collect(DISTINCT product.proper_name) AS proper_names,
       min(product.approval_date) AS first_loaded_approval_date,
       CASE WHEN member = reference THEN true ELSE false END AS is_reference
ORDER BY is_reference DESC, member.bla;
```

Only call a product interchangeable when the observed Purple Book designation says so. `351(k) Biosimilar` alone is not interchangeable status.

## State substitution rule as a separate layer

Use only if the live graph contains versioned state-law data with provenance:

```cypher
MATCH (rule:StateRule {jurisdiction: $state})
WHERE rule.product_class = $product_class
  AND rule.effective_from <= $as_of
  AND (rule.effective_through IS NULL OR rule.effective_through >= $as_of)
RETURN rule.jurisdiction, rule.product_class, rule.summary,
       rule.prescriber_requirements, rule.patient_notice,
       rule.effective_from, rule.effective_through,
       rule.authority_url, rule.retrieved_at
ORDER BY rule.effective_from DESC LIMIT 1;
```

If no such source-backed node exists, report “state-specific eligibility not evaluated.” Do not substitute a general web summary or federal designation for state law.

## Current label per SPL set

A BLA can have several label sets and each set can have versions. Select the newest version within each `set_id`:

```cypher
MATCH (:Application {bla: $bla})-[:HAS_LABEL]->(l:Label)
WITH l.set_id AS set_id, l
ORDER BY l.effective_time DESC, toInteger(l.version) DESC
WITH set_id, collect(l)[0] AS current
RETURN set_id, current.effective_time, current.indications_and_usage,
       current.boxed_warning, current.warnings, current.contraindications
ORDER BY current.effective_time DESC;
```

Report label wording as label evidence, not proof of incidence or causation.

## Retrieve two products for a label difference

Fetch the current documents first; pairing SPL sets is a separate identity decision:

```cypher
UNWIND [{side: 'reference', app: $reference_app},
        {side: 'follow_on', app: $follow_on_app}] AS target
MATCH (application:Application {application_number: target.app})-[:HAS_LABEL]->(label:Label)
WITH target, label.set_id AS set_id, label
ORDER BY label.effective_time DESC, toInteger(label.version) DESC
WITH target, set_id, collect(label)[0] AS current
RETURN target.side, target.app, set_id,
       current.id AS spl_id, current.version, current.effective_time,
       current.brand_names, current.routes,
       current.boxed_warning, current.warnings_and_cautions,
       current.indications_and_usage, current.dosage_forms_and_strengths,
       current.description, current.inactive_ingredient,
       current.dosage_and_administration,
       current.how_supplied, current.storage_and_handling,
       current.source_url
ORDER BY target.side, set_id;
```

Compare sections independently outside Cypher and retain the original text. A missing property means the graph lacks that section; it does not mean the products are equivalent.

## Detect changed sections in a label set

This requires historical label versions to remain in the graph:

```cypher
MATCH (label:Label)
WHERE label.set_id IS NOT NULL
WITH label.set_id AS set_id, label
ORDER BY label.effective_time DESC, toInteger(label.version) DESC
WITH set_id, collect(label)[0..2] AS versions
WHERE size(versions) = 2
WITH set_id, versions[0] AS current, versions[1] AS previous,
     ['boxed_warning', 'warnings_and_cautions', 'contraindications',
      'indications_and_usage', 'dosage_forms_and_strengths', 'description',
      'inactive_ingredient', 'dosage_and_administration', 'how_supplied',
      'storage_and_handling'] AS sections
WITH set_id, current, previous,
     [section IN sections
      WHERE coalesce(current[section], '') <> coalesce(previous[section], '')] AS changed
WHERE size(changed) > 0
RETURN set_id, previous.id AS previous_spl, previous.effective_time AS previous_time,
       current.id AS current_spl, current.effective_time AS current_time,
       changed AS changed_sections
ORDER BY current_time DESC;
```

An alert writer should key alerts by the old/new SPL IDs so reruns are idempotent. Show exact section diffs before claiming a clinically meaningful change.

## Approval and submission history

```cypher
MATCH (:Application {bla: $bla})-[:HAS_SUBMISSION]->(s:Submission)
RETURN s.type, s.number, s.status, s.status_date, s.review_priority,
       s.class_description, s.document_urls
ORDER BY s.status_date DESC LIMIT 100;
```

## Purple Book reference products and biosimilars

Use only imported reference identifiers or entity-resolution edges that record their method and confidence:

```cypher
MATCH (candidate:Application)-[r:REFERENCES]->(reference:Application)
WHERE reference.bla = $reference_bla
RETURN candidate.bla, candidate.applicant, candidate.bla_type,
       r.match_method, r.confidence
ORDER BY candidate.bla;
```

A name-only or ambiguous reference match should remain unresolved rather than becoming a graph edge.

## Recall/enforcement history

Recall `classification`, `status`, and dates describe an enforcement record; they do not imply that every product sharing a name was affected.

```cypher
MATCH (a:Application {bla: $bla})-[:HAS_ENFORCEMENT_REPORT]->(e:EnforcementReport)
WHERE e.recall_initiation_date >= $from AND e.recall_initiation_date <= $through
RETURN e.recall_number, e.classification, e.status, e.reason_for_recall,
       e.recall_initiation_date, e.termination_date, e.product_description
ORDER BY e.recall_initiation_date DESC;
```

If enforcement records were linked through normalized names rather than an identifier, return the relationship's match method and confidence next to every result.

## FAERS: distinct reports over time

Deduplicate follow-up versions before this query, or select only the latest imported version per report identity. Count distinct safety reports, not exploded paths:

```cypher
MATCH (r:SafetyReport)-[:HAS_DRUG]->(d:ReportDrug)-[:MAPS_TO]->(:Application {bla: $bla})
WHERE r.receive_date >= $from AND r.receive_date <= $through
  AND d.role IN $roles
RETURN substring(toString(r.receive_date), 0, 6) AS month,
       count(DISTINCT r) AS distinct_reports
ORDER BY month;
```

Always state the covered dates, deduplication rule, drug roles, and mapping coverage.

## FAERS: co-reported reactions

A report can list multiple drugs and reactions; this query finds report-level co-occurrence only:

```cypher
MATCH (r:SafetyReport)-[:HAS_DRUG]->(d:ReportDrug)-[:MAPS_TO]->(:Application {bla: $bla})
MATCH (r)-[:HAS_REACTION]->(reaction:ReactionTerm)
WHERE r.receive_date >= $from AND r.receive_date <= $through
  AND d.role IN $roles
RETURN reaction.term AS reaction, count(DISTINCT r) AS distinct_reports
ORDER BY distinct_reports DESC LIMIT 25;
```

Say “co-reported with,” not “caused by.” Raw counts cannot estimate incidence or risk because there is no exposure denominator.

## FAERS: reporting odds ratio

Use this only as exploratory signal detection. Define one deduplicated report universe and use `EXISTS` so each report contributes once to the 2×2 table:

```cypher
MATCH (r:SafetyReport)
WHERE r.receive_date >= $from AND r.receive_date <= $through
WITH collect(DISTINCT r) AS universe
UNWIND universe AS r
WITH r,
     EXISTS {
       MATCH (r)-[:HAS_DRUG]->(d:ReportDrug)-[:MAPS_TO]->(:Application {bla: $bla})
       WHERE d.role IN $roles
     } AS has_drug,
     EXISTS {
       MATCH (r)-[:HAS_REACTION]->(rx:ReactionTerm)
       WHERE rx.term = $reaction
     } AS has_reaction
WITH sum(CASE WHEN has_drug AND has_reaction THEN 1 ELSE 0 END) AS a,
     sum(CASE WHEN has_drug AND NOT has_reaction THEN 1 ELSE 0 END) AS b,
     sum(CASE WHEN NOT has_drug AND has_reaction THEN 1 ELSE 0 END) AS c,
     sum(CASE WHEN NOT has_drug AND NOT has_reaction THEN 1 ELSE 0 END) AS d
RETURN a, b, c, d,
       CASE WHEN b = 0 OR c = 0 THEN null ELSE (1.0 * a * d) / (b * c) END AS reporting_odds_ratio;
```

Return the four cells, not only the ratio. Do not interpret a high ROR as causal evidence or comparative clinical safety; sparse cells, reporting bias, confounding, indication, and missing mappings can dominate it.

## Debug an empty result

Strip constraints in this order and record where rows reappear:

```cypher
MATCH (n:CandidateLabel) RETURN count(n);
MATCH (n:CandidateLabel) WHERE n.candidate_id = $id RETURN count(n);
MATCH (n:CandidateLabel)-[r]-() WHERE n.candidate_id = $id
RETURN type(r), count(*) ORDER BY count(*) DESC;
```

Then restore relationship direction, date range/type, role, reaction term, and other filters one at a time. Check identifier punctuation (`125057` versus `BLA125057`), array flattening, null coverage, and snapshot dates before reporting “no data.”
