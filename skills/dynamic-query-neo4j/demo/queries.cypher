// 1) Inventory and provenance
MATCH (n)
RETURN labels(n) AS labels, count(*) AS rows ORDER BY rows DESC;

MATCH (s:DatasetSnapshot)
RETURN s.dataset, s.title, s.source_url, s.retrieved_at,
       s.complete_source_snapshot, s.cohort_terms;

// 2) Resolve a drug name across source-specific records.
// Neo4j Browser parameters: :param term => 'adalimumab';
MATCH (entity)-[r:HAS_NAME]->(name:DrugName)
WHERE name.normalized CONTAINS toLower($term)
RETURN labels(entity) AS source_type, entity, r.kind, r.source, name.display
LIMIT 25;

// 3) Purple Book applications and products, with exact openFDA enrichments.
MATCH (a:Application)-[:HAS_PRODUCT]->(p:BiologicProduct)
OPTIONAL MATCH (a)-[:HAS_LABEL]->(l:Label)
OPTIONAL MATCH (a)-[:HAS_NDC_LISTING]->(n:NdcProduct)
RETURN a.bla, a.applicant, a.bla_type,
       collect(DISTINCT p.proprietary_name) AS purple_book_products,
       count(DISTINCT l) AS labels,
       count(DISTINCT n) AS ndc_listings
ORDER BY a.bla;

// 4) Biosimilar/reference application links resolved only from unique exact
// normalized Purple Book reference names. Missing edges remain unresolved.
MATCH (biosimilar:Application)-[r:REFERENCES]->(reference:Application)
RETURN biosimilar.bla, biosimilar.applicant, biosimilar.bla_type,
       reference.bla, reference.applicant, r.match_method, r.confidence;

// 5) Current label versions for one confirmed BLA.
// :param bla => 'BLA125057';
MATCH (:Application {bla: $bla})-[:HAS_LABEL]->(l:Label)
WITH l.set_id AS set_id, l ORDER BY l.effective_time DESC, toInteger(l.version) DESC
WITH set_id, collect(l)[0] AS current
RETURN set_id, current.effective_time, current.version,
       current.indications_and_usage, current.boxed_warning
ORDER BY current.effective_time DESC;

// 6) Approval/submission history from Drugs@FDA.
MATCH (:Application {bla: $bla})-[:HAS_SUBMISSION]->(s:Submission)
RETURN s.type, s.number, s.status, s.status_date, s.review_priority,
       s.class_description, s.document_urls
ORDER BY s.status_date DESC LIMIT 50;

// 7) NDC packages. An NDC listing does not itself establish FDA approval.
MATCH (:Application {bla: $bla})-[:HAS_NDC_LISTING]->(n:NdcProduct)
OPTIONAL MATCH (n)-[:HAS_PACKAGE]->(p:Package)
RETURN n.product_ndc, n.brand_name, n.generic_name, n.labeler_name,
       n.marketing_category, collect(p.package_ndc) AS packages
ORDER BY n.brand_name;

// 8) Orange Book products with patents and exclusivities.
// :param term => 'semaglutide';
MATCH (p:OrangeBookProduct)-[:HAS_NAME]->(name:DrugName)
WHERE name.normalized CONTAINS toLower($term)
OPTIONAL MATCH (p)-[:HAS_PATENT]->(patent:Patent)
OPTIONAL MATCH (p)-[:HAS_EXCLUSIVITY]->(exclusivity:Exclusivity)
RETURN p.application, p.trade_name, p.ingredient, p.strength, p.te_code,
       collect(DISTINCT {number: patent.number, expiry: patent.expiry}) AS patents,
       collect(DISTINCT {code: exclusivity.code, expiry: exclusivity.expiry}) AS exclusivities
LIMIT 25;

// 9) Exact application + product-number alignments between Purple Book and
// Drugs@FDA. No product-level link is inferred from a display name.
MATCH (pb:BiologicProduct)-[:SAME_APPLICATION_PRODUCT_NUMBER]->(ofda:OpenFDAProduct)
RETURN pb.id, pb.proprietary_name, pb.proper_name,
       ofda.brand_name, ofda.active_ingredients;

// 10) Small-molecule TE cohort. Match ingredient + strength + form/route.
// :param product_id => 'NDA020702:001';
MATCH (selected:OrangeBookProduct {id: $product_id})
MATCH (candidate:OrangeBookProduct)
WHERE candidate.ingredient = selected.ingredient
  AND candidate.strength = selected.strength
  AND candidate.dosage_form_route = selected.dosage_form_route
MATCH (app:Application)-[:HAS_ORANGE_BOOK_PRODUCT]->(candidate)
RETURN app.application_number, candidate.trade_name, candidate.applicant_full_name,
       candidate.approval_date, candidate.rld, candidate.rs, candidate.te_code,
       CASE WHEN candidate.te_code STARTS WITH 'A'
            THEN 'FDA TE evidence present'
            ELSE 'No affirmative A-rated evidence' END AS federal_te_evidence
ORDER BY candidate.rld DESC, candidate.trade_name;

// 11) Retrieve current label sections for a reference/follow-on comparison.
// :param reference_app => 'BLA125057';
// :param follow_on_app => 'BLA761059';
UNWIND [{side: 'reference', app: $reference_app},
        {side: 'follow_on', app: $follow_on_app}] AS target
MATCH (:Application {application_number: target.app})-[:HAS_LABEL]->(l:Label)
WITH target, l.set_id AS set_id, l
ORDER BY l.effective_time DESC, toInteger(l.version) DESC
WITH target, set_id, collect(l)[0] AS current
RETURN target.side, target.app, set_id, current.id AS spl_id,
       current.version, current.effective_time, current.routes,
       current.boxed_warning, current.warnings_and_cautions,
       current.indications_and_usage, current.dosage_forms_and_strengths,
       current.description, current.inactive_ingredient,
       current.dosage_and_administration, current.how_supplied,
       current.storage_and_handling, current.source_url;

// 12) Find changed sections between the newest two retained versions per SPL set.
MATCH (l:Label) WHERE l.set_id IS NOT NULL
WITH l.set_id AS set_id, l
ORDER BY l.effective_time DESC, toInteger(l.version) DESC
WITH set_id, collect(l)[0..2] AS versions
WHERE size(versions) = 2
WITH set_id, versions[0] AS current, versions[1] AS previous,
     ['boxed_warning', 'warnings_and_cautions', 'contraindications',
      'indications_and_usage', 'dosage_forms_and_strengths', 'description',
      'inactive_ingredient', 'dosage_and_administration', 'how_supplied',
      'storage_and_handling'] AS sections
WITH set_id, current, previous,
     [s IN sections WHERE coalesce(current[s], '') <> coalesce(previous[s], '')] AS changed
WHERE size(changed) > 0
RETURN set_id, previous.id, previous.effective_time,
       current.id, current.effective_time, changed
ORDER BY current.effective_time DESC;
