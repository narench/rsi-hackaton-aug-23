from __future__ import annotations

import os
import re
import time
from typing import Any

from neo4j import GraphDatabase, Query

from .models import QueryPlan, ResolvedIdentity, ValidationResult

FORBIDDEN = re.compile(
    r"\b(CALL|CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD\s+CSV|FOREACH|GRANT|DENY|REVOKE|ALTER|START\s+DATABASE|STOP\s+DATABASE)\b",
    re.IGNORECASE,
)
FIELD_EXPRESSIONS = {
    "ingredient": "p.ingredient",
    "strength": "p.strength",
    "dosage_form_route": "p.dosage_form_route",
    "te_code": "p.te_code",
}


class GraphExecutor:
    def __init__(self, uri: str | None = None, username: str | None = None,
                 password: str | None = None):
        self.driver = GraphDatabase.driver(
            uri or os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            auth=(username or os.getenv("NEO4J_USERNAME", "neo4j"),
                  password or os.getenv("NEO4J_PASSWORD", "openfda-demo")),
        )
        self.driver.verify_connectivity()

    def close(self):
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        if FORBIDDEN.search(cypher) or "//" in cypher or "/*" in cypher or "*/" in cypher:
            raise ValueError("generated Cypher is outside the read-only allowlist")
        if not cypher.lstrip().upper().startswith(("MATCH", "OPTIONAL MATCH", "UNWIND")):
            raise ValueError("generated Cypher must start with a read-only clause")
        if ";" in cypher.strip().rstrip(";"):
            raise ValueError("multiple Cypher statements are not allowed")
        started = time.perf_counter()
        with self.driver.session() as session:
            result = session.run(Query(cypher, timeout=15.0), **params)
            rows = [row.data() for row in result]
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        if len(rows) > 100:
            raise ValueError("query exceeded the 100-row execution boundary")
        return rows

    def resolve_name(self, question: str) -> str | None:
        text = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
        rows = self.run("""
        MATCH (name:DrugName)
        WHERE $text CONTAINS name.normalized
        RETURN name.normalized AS name, size(name.normalized) AS specificity
        ORDER BY specificity DESC LIMIT 1
        """, text=text)
        return rows[0]["name"] if rows else None

    def resolve_orange_identity(self, question: str, term: str) -> ResolvedIdentity | None:
        lower = question.lower()
        strength_match = re.search(r"(\d+(?:\.\d+)?)\s*mg", lower)
        strength_hint = f"{strength_match.group(1)}MG" if strength_match else None
        form_hint = "TABLET" if "tablet" in lower else ("CAPSULE" if "capsule" in lower else None)
        route_hint = "ORAL" if "oral" in lower else None
        rows = self.run("""
        MATCH (p:OrangeBookProduct)-[:HAS_NAME]->(:DrugName {normalized: $term})
        WHERE ($strength IS NULL
           OR replace(toUpper(coalesce(p.strength, '')), ' ', '') CONTAINS $strength)
          AND ($form IS NULL OR toUpper(coalesce(p.dosage_form_route, '')) CONTAINS $form)
          AND ($route IS NULL OR toUpper(coalesce(p.dosage_form_route, '')) CONTAINS $route)
        RETURN DISTINCT p.id AS id, p.ingredient AS ingredient, p.strength AS strength,
               p.dosage_form_route AS dosage_form_route, p.trade_name AS trade_name,
               p.rld AS rld, p.te_code AS te_code, p.approval_date AS approval_date
        ORDER BY CASE WHEN rld = 'Yes' THEN 0 ELSE 1 END, approval_date
        LIMIT 30
        """, term=term, strength=strength_hint, form=form_hint, route=route_hint)
        if not rows:
            return None
        selected = rows[0]
        forms = {row["dosage_form_route"] for row in rows if row.get("dosage_form_route")}
        te_codes = {row["te_code"] for row in rows if row.get("te_code")}
        return ResolvedIdentity(
            term=term,
            ingredient=selected["ingredient"],
            strength=selected["strength"] if strength_hint else None,
            dosage_form_route=(next(iter(forms)) if strength_hint and len(forms) == 1 else None),
            te_code=(selected.get("te_code") if strength_hint and len(te_codes) >= 1 else None),
            selected_product_id=selected["id"],
            alternatives=rows,
        )


def build_substitution_plan(identity: ResolvedIdentity, cohort_fields: list[str]) -> QueryPlan:
    clauses = []
    parameters: dict[str, Any] = {}
    for field in cohort_fields:
        if field not in FIELD_EXPRESSIONS:
            raise ValueError(f"unsupported cohort field: {field}")
        value = getattr(identity, field)
        if value is None:
            raise ValueError(f"missing identity field: {field}")
        clauses.append(f"{FIELD_EXPRESSIONS[field]} = ${field}")
        parameters[field] = value
    where = " AND ".join(clauses)
    cypher = f"""
    MATCH (p:OrangeBookProduct)
    WHERE {where}
    MATCH (a:Application)-[:HAS_ORANGE_BOOK_PRODUCT]->(p)
    RETURN a.application_number AS application, p.id AS product_id,
           p.trade_name AS product, p.ingredient AS ingredient,
           p.strength AS strength, p.dosage_form_route AS dosage_form_route,
           p.applicant_full_name AS manufacturer, p.approval_date AS approval,
           p.rld AS rld, p.rs AS rs, p.te_code AS te_code
    ORDER BY CASE WHEN p.rld = 'Yes' THEN 0 ELSE 1 END, p.trade_name
    LIMIT 100
    """
    return QueryPlan(
        intent="substitution",
        template_id="orange-book-substitution",
        cypher=cypher,
        parameters=parameters,
        cohort_fields=cohort_fields,
        expected_grain=["ingredient", "strength", "dosage_form_route"],
    )


def validate_substitution(rows: list[dict]) -> ValidationResult:
    if not rows:
        return ValidationResult(
            passed=False, failures=["EMPTY_RESULT"], metrics={"row_count": 0}, repairable=True
        )
    ingredients = {row.get("ingredient") for row in rows}
    strengths = {row.get("strength") for row in rows}
    forms = {row.get("dosage_form_route") for row in rows}
    te_codes = {row.get("te_code") for row in rows}
    failures = []
    if len(ingredients) != 1:
        failures.append("MIXED_INGREDIENT")
    if len(strengths) != 1:
        failures.append("MIXED_STRENGTH")
    if len(forms) != 1:
        failures.append("MIXED_FORM_ROUTE")
    metrics = {
        "row_count": len(rows),
        "distinct_ingredients": len(ingredients),
        "distinct_strengths": len(strengths),
        "distinct_form_routes": len(forms),
        "distinct_te_codes": len(te_codes),
        "a_rated_non_reference": sum(
            bool(row.get("te_code") and row["te_code"].startswith("A") and row.get("rld") != "Yes")
            for row in rows
        ),
        "missing_te_code": sum(row.get("te_code") is None for row in rows),
    }
    return ValidationResult(
        passed=not failures,
        failures=failures,
        metrics=metrics,
        repairable=bool(failures) and all(
            failure in {"MIXED_INGREDIENT", "MIXED_STRENGTH", "MIXED_FORM_ROUTE"}
            for failure in failures
        ),
    )
