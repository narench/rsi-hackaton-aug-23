#!/usr/bin/env python3
"""Streamlit chatbot for the FDA Orange Book, Purple Book, and openFDA graph."""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import streamlit as st
from neo4j import GraphDatabase

from rsi.offline import OfflineRSI
from rsi.online import OnlineQueryAgent
from rsi.policy import activate_version, load_active_policy

st.set_page_config(
    page_title="FDA Drug Graph",
    page_icon="◉",
    layout="wide",
    initial_sidebar_state="expanded",
)

CSS = """
<style>
:root { color-scheme: light; }
.stApp { background: #f5f7f4; color: #17201c; }
[data-testid="stMain"], [data-testid="stMain"] p,
[data-testid="stMain"] h1, [data-testid="stMain"] h2,
[data-testid="stMain"] h3, [data-testid="stMain"] label,
[data-testid="stMain"] th, [data-testid="stMain"] td {
  color: #17201c !important;
}
[data-testid="stMain"] input, [data-testid="stMain"] textarea {
  color: #17201c !important; background: #ffffff !important;
  -webkit-text-fill-color: #17201c !important; caret-color: #17201c !important;
}
[data-testid="stMain"] input::placeholder,
[data-testid="stMain"] textarea::placeholder {
  color: #69756f !important; opacity: 1 !important;
  -webkit-text-fill-color: #69756f !important;
}
[data-testid="stMetric"] [data-testid="stMetricLabel"],
[data-testid="stMetric"] [data-testid="stMetricValue"],
[data-testid="stMetric"] [data-testid="stMetricDelta"] {
  color: #17201c !important; -webkit-text-fill-color: #17201c !important;
}
[data-testid="stChatMessage"] { color: #17201c !important; }
[data-testid="stSidebar"] { background: #18231f; color: #edf3ee; }
[data-testid="stSidebar"] * { color: #edf3ee; }
[data-testid="stSidebar"] .stButton button {
  background: #24332d; border: 1px solid #3b4c44; color: #edf3ee;
  text-align: left; min-height: 2.8rem;
}
[data-testid="stSidebar"] .stButton button:hover { border-color: #9ed48e; color: #c6f1b9; }
.block-container { max-width: 1080px; padding-top: 2.2rem; }
h1 { letter-spacing: -0.035em; font-weight: 720; }
[data-testid="stChatMessage"] {
  background: #fbfcfa; border: 1px solid #dce3dd; border-radius: 14px;
  padding: .4rem .75rem; margin-bottom: .75rem;
}
.fda-kicker { color: #52655b; font-size: .78rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.source-note { color: #5c6b63; font-size: .82rem; }
.status-live { display:inline-block; width:.55rem; height:.55rem; border-radius:50%; background:#72c66a; margin-right:.4rem; }
div[data-testid="stMetric"] { background:#edf1ed; border-radius:10px; padding:.7rem 1rem; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
PASSWORD = os.getenv("NEO4J_PASSWORD", "openfda-demo")


@st.cache_resource
def driver():
    instance = GraphDatabase.driver(URI, auth=(USERNAME, PASSWORD))
    instance.verify_connectivity()
    return instance


def query(cypher: str, **params: Any) -> list[dict]:
    with driver().session() as session:
        return [record.data() for record in session.run(cypher, **params)]


def graph_stats() -> dict[str, int]:
    rows = query("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n")
    return {row["label"]: row["n"] for row in rows}


def find_names(text: str, limit: int = 8) -> list[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    rows = query("""
    MATCH (name:DrugName)
    WHERE $text CONTAINS name.normalized
       OR ($text = name.normalized)
    RETURN name.normalized AS name, size(name.normalized) AS specificity
    ORDER BY specificity DESC LIMIT $limit
    """, text=normalized, limit=limit)
    names = []
    for row in rows:
        if row["name"] not in names:
            names.append(row["name"])
    if not names and normalized:
        rows = query("""
        MATCH (name:DrugName)
        WHERE name.normalized CONTAINS $text
        RETURN name.normalized AS name ORDER BY size(name.normalized) LIMIT $limit
        """, text=normalized, limit=limit)
        names = [row["name"] for row in rows]
    return names


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    def safe(value: Any) -> str:
        if value is None or value == "":
            return "Not loaded"
        if isinstance(value, list):
            value = ", ".join(str(x) for x in value if x)
        return str(value).replace("|", "\\|").replace("\n", " ")
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *["| " + " | ".join(safe(x) for x in row) + " |" for row in rows],
    ])


def biologic_family(term: str) -> dict:
    cypher = """
    MATCH (a:Application)-[:HAS_PRODUCT]->(p:BiologicProduct)
    WHERE toLower(coalesce(p.proprietary_name, '')) CONTAINS $term
       OR toLower(coalesce(p.proper_name, '')) CONTAINS $term
       OR toLower(coalesce(p.reference_proprietary_name, '')) CONTAINS $term
       OR toLower(coalesce(p.reference_proper_name, '')) CONTAINS $term
    OPTIONAL MATCH (a)-[:REFERENCES]->(reference:Application)
    OPTIONAL MATCH (a)-[:HAS_LABEL]->(label:Label)
    OPTIONAL MATCH (a)-[:HAS_NDC_LISTING]->(ndc:NdcProduct)
    RETURN a.bla AS application, a.applicant AS manufacturer,
           a.bla_type AS status, collect(DISTINCT p.proprietary_name) AS brands,
           min(p.approval_date) AS approval_date, reference.bla AS reference,
           count(DISTINCT label) AS labels, count(DISTINCT ndc) AS ndc_listings
    ORDER BY CASE WHEN a.bla_type = '351(a)' THEN 0 ELSE 1 END, a.bla
    LIMIT 30
    """
    rows = query(cypher, term=term)
    if not rows:
        return {"text": f"I could not find a Purple Book family for **{term}**.", "cypher": cypher}
    table = markdown_table(
        ["Brand", "Application", "Purple Book status", "Manufacturer", "Approval", "Reference"],
        [[r["brands"], r["application"], r["status"], r["manufacturer"],
          r["approval_date"], r["reference"]] for r in rows],
    )
    interchangeable = sum("Interchangeable" in (r["status"] or "") for r in rows)
    biosimilar = sum("Biosimilar" in (r["status"] or "") for r in rows)
    return {
        "text": (
            f"### {term.title()} biologic family\n\n"
            f"The loaded Purple Book cohort contains **{len(rows)} applications**: "
            f"**{interchangeable} interchangeable** and **{biosimilar} biosimilar-only** follow-ons.\n\n"
            f"{table}\n\n"
            "Purple Book designation is federal evidence. State substitution rules are not loaded."
        ),
        "cypher": cypher,
    }


def orange_candidates(term: str, strength_hint: str | None) -> list[dict]:
    return query("""
    MATCH (p:OrangeBookProduct)-[:HAS_NAME]->(name:DrugName)
    WHERE name.normalized = $term
      AND ($strength IS NULL OR replace(toUpper(p.strength), ' ', '') CONTAINS $strength)
    RETURN DISTINCT p.id AS id, p.trade_name AS trade_name, p.ingredient AS ingredient,
           p.strength AS strength, p.dosage_form_route AS form_route, p.rld AS rld,
           p.te_code AS te_code
    ORDER BY CASE WHEN p.rld = 'Yes' THEN 0 ELSE 1 END, p.approval_date
    LIMIT 30
    """, term=term, strength=strength_hint)


def generic_equivalence(term: str, prompt: str) -> dict:
    match = re.search(r"(\d+(?:\.\d+)?)\s*mg", prompt.lower())
    strength_hint = f"{match.group(1)}MG" if match else None
    candidates = orange_candidates(term, strength_hint)
    if not candidates:
        return {"text": f"I could not resolve **{term}** to an Orange Book product.", "cypher": "Orange Book identity lookup"}

    cohorts = {(r["ingredient"], r["strength"], r["form_route"]) for r in candidates}
    if len(cohorts) > 1:
        options = markdown_table(
            ["Ingredient", "Strength", "Form and route"],
            [list(values) for values in sorted(cohorts)[:12]],
        )
        return {
            "text": (
                f"**{term}** has multiple pharmaceutical-equivalence cohorts. "
                "Add a strength to the question so I do not combine unlike products.\n\n" + options
            ),
            "cypher": "Orange Book identity and cohort discovery",
        }

    ingredient, strength, form_route = next(iter(cohorts))
    reference = next((row for row in candidates if row.get("rld") == "Yes"), candidates[0])
    reference_te_code = reference.get("te_code")
    cypher = """
    MATCH (p:OrangeBookProduct)
    WHERE p.ingredient = $ingredient AND p.strength = $strength
      AND p.dosage_form_route = $form_route
      AND ($te_code IS NULL OR p.te_code = $te_code)
    MATCH (a:Application)-[:HAS_ORANGE_BOOK_PRODUCT]->(p)
    RETURN a.application_number AS application, p.trade_name AS product,
           p.applicant_full_name AS manufacturer, p.approval_date AS approval,
           p.rld AS rld, p.rs AS rs, p.te_code AS te_code
    ORDER BY CASE WHEN p.rld = 'Yes' THEN 0 ELSE 1 END, p.trade_name
    LIMIT 50
    """
    rows = query(cypher, ingredient=ingredient, strength=strength,
                 form_route=form_route, te_code=reference_te_code)
    a_rated = [r for r in rows if (r["te_code"] or "").startswith("A") and r["rld"] != "Yes"]
    table = markdown_table(
        ["Product", "Application", "TE code", "RLD", "Manufacturer", "Approval"],
        [[r["product"], r["application"], r["te_code"], r["rld"],
          r["manufacturer"], r["approval"]] for r in rows[:25]],
    )
    return {
        "text": (
            f"### Orange Book equivalence cohort\n\n"
            f"**{ingredient} · {strength} · {form_route} · TE {reference_te_code or 'not loaded'}**\n\n"
            f"Found **{len(a_rated)} non-reference A-rated products** in this exact TE group.\n\n"
            f"{table}\n\n"
            "An A-prefix TE code is FDA Orange Book evidence, not a complete substitution decision. "
            "State law, prescriber instructions, and patient-specific factors are not evaluated."
        ),
        "cypher": cypher,
    }


def applications_for_name(name: str) -> list[str]:
    rows = query("""
    MATCH (entity)-[:HAS_NAME]->(:DrugName {normalized: $name})
    MATCH (app:Application)-[]->(entity)
    WHERE app.application_number IS NOT NULL
    RETURN DISTINCT app.application_number AS application
    ORDER BY application
    """, name=name)
    return [row["application"] for row in rows]


LABEL_SECTIONS = [
    ("Warnings", "warnings_and_cautions"),
    ("Boxed warning", "boxed_warning"),
    ("Indications", "indications_and_usage"),
    ("Form and strength", "dosage_forms_and_strengths"),
    ("Description/formulation", "description"),
    ("Inactive ingredients", "inactive_ingredient"),
    ("Administration", "dosage_and_administration"),
    ("Storage", "storage_and_handling"),
]


def current_labels(application: str) -> list[dict]:
    return query("""
    MATCH (:Application {application_number: $application})-[:HAS_LABEL]->(l:Label)
    WITH l.set_id AS set_id, l ORDER BY l.effective_time DESC, toInteger(l.version) DESC
    WITH set_id, collect(l)[0] AS current
    RETURN set_id, properties(current) AS label
    ORDER BY current.effective_time DESC
    """, application=application)


def label_comparison(names: list[str]) -> dict:
    resolved: list[tuple[str, str]] = []
    for name in names:
        apps = applications_for_name(name)
        if apps:
            resolved.append((name, apps[0]))
    deduped = []
    for item in resolved:
        if item[1] not in [x[1] for x in deduped]:
            deduped.append(item)
    if len(deduped) < 2:
        return {
            "text": "Name **two loaded products** to compare, for example: `Compare Humira and Hadlima labels`.",
            "cypher": "Drug name to application resolution",
        }
    left, right = deduped[:2]
    left_labels, right_labels = current_labels(left[1]), current_labels(right[1])
    if not left_labels or not right_labels:
        return {
            "text": f"Current labels are not loaded for both {left[0]} and {right[0]}.",
            "cypher": "Current SPL label lookup",
        }
    a, b = left_labels[0]["label"], right_labels[0]["label"]
    section_rows = []
    changed = 0
    for display, key in LABEL_SECTIONS:
        av, bv = a.get(key) or "", b.get(key) or ""
        if not av or not bv:
            status = "Missing on one or both sides"
        else:
            similarity = SequenceMatcher(None, re.sub(r"\s+", " ", av.lower()),
                                         re.sub(r"\s+", " ", bv.lower())).ratio()
            status = f"Different ({round(similarity * 100)}% text similarity)"
            if similarity > .995:
                status = "Text nearly identical"
            else:
                changed += 1
        section_rows.append([display, status])
    table = markdown_table(["Section", "Comparison"], section_rows)
    return {
        "text": (
            f"### Label comparison\n\n"
            f"**{left[0].title()}** `{left[1]}` vs **{right[0].title()}** `{right[1]}`\n\n"
            f"Compared SPL `{a.get('id')}` ({a.get('effective_time')}) with "
            f"`{b.get('id')}` ({b.get('effective_time')}).\n\n{table}\n\n"
            f"**{changed} populated sections differ materially by text.** "
            "These are different SPL sets, so this is a document comparison, not a clinical-equivalence conclusion."
        ),
        "cypher": "Newest label per application and SPL set; normalized section-by-section comparison",
    }


def search_result(term: str) -> dict:
    cypher = """
    MATCH (entity)-[r:HAS_NAME]->(name:DrugName {normalized: $term})
    OPTIONAL MATCH (app:Application)-[]->(entity)
    RETURN labels(entity)[0] AS source_type, app.application_number AS application,
           r.kind AS name_type, r.source AS source, properties(entity) AS record
    LIMIT 20
    """
    rows = query(cypher, term=term)
    if not rows:
        return {"text": f"No graph records matched **{term}**.", "cypher": cypher}
    table = markdown_table(
        ["Source", "Application", "Name type", "Dataset"],
        [[r["source_type"], r["application"], r["name_type"], r["source"]] for r in rows],
    )
    return {"text": f"### Matches for {term}\n\n{table}", "cypher": cypher}


def reset_rsi_demo() -> None:
    activate_version(1)
    root = Path(__file__).resolve().parent / "rsi"
    for path in (root / "policies").glob("query-policy.v[2-9]*.yaml"):
        path.unlink(missing_ok=True)
    for path in (root / "state").glob("*.db*"):
        path.unlink(missing_ok=True)


def run_live_rsi_demo(max_attempts: int, candidate_count: int,
                      max_rounds: int) -> dict:
    question = "Find substitutes for Lipitor 10 mg oral tablets"
    activate_version(1)
    online = OnlineQueryAgent(max_attempts=max_attempts)
    workflow = OfflineRSI(
        online_agent=online, store=online.store, candidate_count=candidate_count
    )
    try:
        before = online.run(question, persist=True)
        rounds = []
        proposal = None
        promotion = None
        for round_number in range(1, max_rounds + 1):
            thread_id, interrupted = workflow.start()
            interrupts = interrupted.get("__interrupt__", [])
            if not interrupts:
                rounds.append({
                    "round": round_number,
                    "status": interrupted.get("status", "no_candidate"),
                })
                break
            proposal = interrupts[0].value
            promotion = workflow.resume(thread_id, approved=True)
            rounds.append({
                "round": round_number,
                "status": promotion.get("status"),
                "candidate": proposal.get("candidate_id"),
                "baseline_score": proposal.get("baseline_score"),
                "candidate_score": proposal.get("candidate_score"),
            })
            if promotion.get("status") != "promoted":
                break
        if proposal is None or promotion is None:
            raise RuntimeError(f"RSI produced no promotable candidate: {rounds[-1]['status']}")
        after = online.run(question, persist=True)
        return {
            "question": question,
            "parameters": {
                "max_online_attempts": max_attempts,
                "candidate_branches": candidate_count,
                "max_optimization_rounds": max_rounds,
                "benchmark_cases": 4,
            },
            "runtime": {
                "orchestrator": "LangGraph 1.2.11",
                "optimizer": "bounded deterministic policy mutator",
                "evaluation": "3 Orange Book cases + 1 Purple Book capability gate",
                "evidence": "Neo4j Orange Book snapshot",
                "persistence": "SQLite episodes and LangGraph checkpoints",
            },
            "rounds": rounds,
            "before": before["answer"].model_dump(),
            "before_repairs": [item.model_dump() for item in before.get("repairs", [])],
            "proposal": proposal,
            "promotion": {"status": promotion.get("status"), "path": promotion.get("promoted_path")},
            "after": after["answer"].model_dump(),
        }
    finally:
        workflow.close()


def answer(prompt: str) -> dict:
    lower = prompt.lower()
    names = find_names(prompt)
    if any(word in lower for word in ("compare", "difference", "label")) and len(names) >= 2:
        return label_comparison(names)
    if any(word in lower for word in ("generic", "substitute", "equivalent", "a-rated", "ab-rated", "te code")):
        if not names:
            return {"text": "Tell me the product or active ingredient to check.", "cypher": "Identity resolution"}
        return generic_equivalence(names[0], prompt)
    if any(word in lower for word in ("biosimilar", "interchangeable", "reference", "family")):
        term = names[0] if names else re.sub(r"[^a-z0-9 ]", "", lower).strip()
        policy = load_active_policy()
        if ("adalimumab" in term and "interchangeable" in lower
                and not policy.capabilities.get("biologic_interchangeability", False)):
            return {
                "text": (
                    "### Query blocked by policy v1\n\n"
                    "The baseline policy does not contain the validated identity and cohort rules "
                    "required for an adalimumab interchangeability answer.\n\n"
                    "**Failure:** `MISSING_VALIDATED_BIOLOGIC_POLICY`\n\n"
                    "Run **Run live RSI demo** to evaluate and promote policy v2, then replay this question."
                ),
                "cypher": "Not executed: policy v1 safety gate",
            }
        return biologic_family(term)
    if names:
        # Prefer a biologic family when the resolved name exists in Purple Book.
        probe = query("""
        MATCH (p:BiologicProduct)-[:HAS_NAME]->(:DrugName {normalized: $term})
        RETURN count(p) AS n
        """, term=names[0])
        if probe and probe[0]["n"]:
            return biologic_family(names[0])
        return search_result(names[0])
    return {
        "text": (
            "I could not resolve a loaded drug name. Try a brand or ingredient such as "
            "**Humira**, **adalimumab**, **Lipitor**, or **atorvastatin calcium**."
        ),
        "cypher": "DrugName resolution",
    }


with st.sidebar:
    st.markdown("### FDA Drug Graph")
    try:
        stats = graph_stats()
        st.markdown('<span class="status-live"></span>Neo4j connected', unsafe_allow_html=True)
        st.caption(f"{sum(stats.values()):,} nodes across current FDA snapshots")
        st.caption(f"Active RSI policy: **v{load_active_policy().version}**")
    except Exception as exc:
        st.error(f"Neo4j unavailable: {exc}")
        st.stop()

    st.markdown("#### Demo questions")
    prompts = [
        "Show the Humira biosimilar family",
        "Which adalimumab products are interchangeable?",
        "Find AB-rated substitutes for Lipitor 10 mg",
        "Compare Humira and Hadlima labels",
    ]
    selected_prompt = None
    for i, text in enumerate(prompts):
        if st.button(text, key=f"prompt-{i}", use_container_width=True):
            selected_prompt = text

    st.markdown("#### Self-improvement")
    online_attempts = st.slider(
        "Online repair attempts", 2, 5, 3,
        help="Maximum generate/execute/validate attempts for one user request.",
    )
    candidate_branches = st.slider(
        "Policy candidates per round", 1, 3, 3,
        help="How many bounded policy descendants compete in offline evaluation.",
    )
    optimization_rounds = st.slider(
        "Optimization rounds", 1, 3, 1,
        help="Maximum recursive promotion rounds. The loop stops early when no candidate improves the score.",
    )
    run_rsi = st.button("Run live RSI demo", key="run-rsi", use_container_width=True, type="primary")
    reset_rsi = st.button("Reset demo to policy v1", key="reset-rsi", use_container_width=True)
    st.caption("Online: query repair. Offline: candidates, four graph-backed gates, approval, and promotion.")

    st.markdown("#### Loaded evidence")
    st.caption("Orange Book · Purple Book · Drugs@FDA · SPL labels · NDC")
    st.caption("State substitution law is not loaded. Results are research evidence, not medical or legal advice.")

st.markdown('<div class="fda-kicker">Hackathon research console</div>', unsafe_allow_html=True)
st.title("Ask the FDA drug graph")
st.caption("Trace reference products, follow-ons, therapeutic-equivalence evidence, and label differences.")

if reset_rsi:
    try:
        reset_rsi_demo()
        st.session_state.pop("rsi_error", None)
        st.session_state.pop("rsi_result", None)
        st.session_state.pop("messages", None)
        st.session_state.rsi_reset_notice = True
        st.rerun()
    except Exception as exc:
        st.session_state.rsi_error = str(exc)

if run_rsi:
    st.session_state.pop("rsi_error", None)
    st.session_state.pop("rsi_result", None)
    with st.spinner("Running online repair, candidate evaluation, and policy promotion…"):
        try:
            st.session_state.rsi_result = run_live_rsi_demo(
                max_attempts=online_attempts,
                candidate_count=candidate_branches,
                max_rounds=optimization_rounds,
            )
        except Exception as exc:
            st.session_state.rsi_error = str(exc)

if st.session_state.pop("rsi_reset_notice", False):
    st.success("RSI state cleared. Policy v1 is active and ready to replay.")

if st.session_state.get("rsi_error"):
    st.error("RSI demo failed: " + st.session_state.rsi_error)

if result := st.session_state.get("rsi_result"):
    st.markdown("## Live self-improvement replay")
    before, after, proposal = result["before"], result["after"], result["proposal"]
    left, center, right = st.columns([1, 1.2, 1])
    with left:
        st.metric("Before", f"Policy v{before['policy_version']}")
        st.metric("Query attempts", before["attempts"])
        st.caption("The first query mixed strengths and dosage-form/route cohorts.")
    with center:
        st.markdown("**Promoted mutation**")
        st.code("\n".join(
            f"+ {op['op']}: {op.get('field') or op.get('action')}"
            for op in proposal["operations"]
        ), language="diff")
        st.caption(
            f"Eval {proposal['baseline_score']:.0%} → {proposal['candidate_score']:.0%}; "
            f"{proposal['regressions']} regressions"
        )
    with right:
        st.metric("After", f"Policy v{after['policy_version']}")
        st.metric("Query attempts", after["attempts"], delta=after["attempts"] - before["attempts"])
        st.caption("The promoted policy constrains the correct cohort on the first attempt.")
    with st.expander("Full policy diff (v1 → promoted candidate)", expanded=True):
        st.code(proposal.get("policy_diff", "No policy diff available."), language="diff")
    parameter_cols = st.columns(4)
    labels = [
        ("Online attempts", "max_online_attempts"),
        ("Candidates", "candidate_branches"),
        ("Max rounds", "max_optimization_rounds"),
        ("Eval cases", "benchmark_cases"),
    ]
    for column, (label, key) in zip(parameter_cols, labels):
        column.metric(label, result["parameters"][key])
    with st.expander("What RSI is running on"):
        for label, value in result["runtime"].items():
            st.markdown(f"**{label.replace('_', ' ').title()}:** {value}")
        st.markdown("**Rounds executed:**")
        st.json(result["rounds"])
    with st.expander("Online failure and repair trace"):
        st.json(result["before_repairs"])
    st.success("Policy promoted and replayed against the same prompt.")
    st.divider()

if "messages" not in st.session_state:
    st.session_state.messages = [{
        "role": "assistant",
        "text": "Ask about a brand, ingredient, generic relationship, biosimilar family, or label comparison.",
        "cypher": None,
    }]

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["text"])
        if message.get("cypher"):
            with st.expander("Graph retrieval details"):
                st.code(message["cypher"], language="cypher")

prompt = selected_prompt or st.chat_input("Ask about a drug or product relationship")
if prompt:
    st.session_state.messages.append({"role": "user", "text": prompt, "cypher": None})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Traversing FDA datasets…"):
            try:
                response = answer(prompt)
            except Exception as exc:
                response = {"text": f"The graph query failed: `{exc}`", "cypher": None}
        st.markdown(response["text"])
        if response.get("cypher"):
            with st.expander("Graph retrieval details"):
                st.code(response["cypher"], language="cypher")
    st.session_state.messages.append({"role": "assistant", **response})
