#!/usr/bin/env python3
"""Load a bounded Purple Book + openFDA biologics research graph into Neo4j."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from neo4j import GraphDatabase

PURPLEBOOK_DOWNLOADS = "https://purplebooksearch.fda.gov/index.cfm?event=downloads"
PURPLEBOOK_DEFAULT = "latest"
ORANGEBOOK_DEFAULT = "https://www.fda.gov/media/76860/download"
OPENFDA_ROOT = "https://api.fda.gov/drug"
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc


PURPLEBOOK_URL = os.getenv("PURPLEBOOK_URL", PURPLEBOOK_DEFAULT)
ORANGEBOOK_URL = os.getenv("ORANGEBOOK_URL", ORANGEBOOK_DEFAULT)
SEED_TERMS = [x.strip() for x in os.getenv(
    "SEED_TERMS", "adalimumab,trastuzumab,pembrolizumab"
).split(",") if x.strip()]
MAX_APPLICATIONS = env_int("MAX_APPLICATIONS", 20)
OPENFDA_LIMIT = min(max(env_int("OPENFDA_LIMIT", 100), 1), 1000)
API_KEY = os.getenv("OPENFDA_API_KEY", "").strip()


def clean(value: Any) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return None if not value or value.upper() in {"N/A", "NA", "NULL"} else value


def normalize_name(value: str | None) -> str | None:
    value = clean(value)
    if not value:
        return None
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip() or None


def normalize_bla(value: str | None) -> str | None:
    """Convert Purple Book 125057/017055 to openFDA BLA125057 without losing zeros."""
    value = clean(value)
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return f"BLA{digits.zfill(6)}" if digits else None


def scalar_list(values: Iterable[Any]) -> list[str]:
    return [v for item in values if (v := clean(item))]


def fetch_bytes(url: str, cache_name: str, refresh: bool = False) -> bytes:
    path = DATA_DIR / cache_name
    if path.exists() and not refresh:
        print(f"cache hit: {path}")
        return path.read_bytes()
    response = requests.get(url, timeout=90, headers={"User-Agent": "openfda-neo4j-demo/1.0"})
    response.raise_for_status()
    path.write_bytes(response.content)
    print(f"downloaded: {url} -> {path}")
    return response.content


def latest_purple_book() -> tuple[str, bytes]:
    page = fetch_bytes(PURPLEBOOK_DOWNLOADS, "purplebook-downloads.html", refresh=True)
    links = re.findall(rb'href=["\']([^"\']+\.csv)["\']', page, flags=re.I)
    month_numbers = {name.lower(): i for i, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"), 1)}
    candidates = []
    for raw in links:
        url = raw.decode("utf-8", errors="ignore")
        year_match = re.search(r"/(20\d{2})/", url)
        month_match = re.search(r"-(january|february|march|april|may|june|july|august|september|october|november|december)-data", url, re.I)
        if year_match and month_match:
            candidates.append(((int(year_match.group(1)), month_numbers[month_match.group(1).lower()]), url))
    for (_, url) in sorted(set(candidates), reverse=True):
        try:
            payload = fetch_bytes(
                url, f"purplebook-{hashlib.sha256(url.encode()).hexdigest()[:12]}.csv",
                refresh=True,
            )
            parse_purple_book(payload)  # reject broken/future placeholder links
            return url, payload
        except (requests.RequestException, ValueError):
            continue
    raise ValueError("Could not discover a valid Purple Book CSV from the FDA download page")


def parse_purple_book(payload: bytes) -> tuple[str, list[dict[str, str | None]]]:
    text = payload.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    header_indexes = [
        i for i, row in enumerate(rows)
        if len(row) >= 5 and "BLA Number" in row and "Product Number" in row
    ]
    if not header_indexes:
        raise ValueError("Purple Book header not found; the export schema may have changed")

    # FDA monthly reports contain changes first and the complete snapshot second.
    header_index = header_indexes[-1]
    headers = [clean(x) or f"column_{i}" for i, x in enumerate(rows[header_index])]
    parsed: list[dict[str, str | None]] = []
    for raw in rows[header_index + 1:]:
        padded = raw + [""] * (len(headers) - len(raw))
        record = {headers[i]: clean(padded[i]) for i in range(len(headers))}
        if normalize_bla(record.get("BLA Number")) and record.get("Product Number"):
            parsed.append(record)

    if not parsed:
        raise ValueError("Purple Book full-database section contained no product rows")
    return clean(rows[0][0]) or "Purple Book snapshot", parsed


def select_cohort(rows: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    terms = [normalize_name(x) for x in SEED_TERMS]
    terms = [x for x in terms if x]
    fields = (
        "Proprietary Name", "Proper Name", "Ref. Product Proper Name",
        "Ref. Product Proprietary Name",
    )

    def matches(row: dict[str, str | None]) -> bool:
        haystack = " | ".join(normalize_name(row.get(f)) or "" for f in fields)
        return any(term in haystack for term in terms)

    selected = [row for row in rows if matches(row)]
    blas: list[str] = []
    for row in selected:
        bla = normalize_bla(row.get("BLA Number"))
        if bla and bla not in blas:
            blas.append(bla)
    allowed = set(blas[:MAX_APPLICATIONS])
    result = [r for r in selected if normalize_bla(r.get("BLA Number")) in allowed]
    if not result:
        raise ValueError(f"No Purple Book rows matched SEED_TERMS={SEED_TERMS!r}")
    return result


def parse_orange_book(payload: bytes) -> tuple[list[dict], list[dict], list[dict]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        def table(name: str) -> list[dict[str, str | None]]:
            text = archive.read(name).decode("latin-1", errors="replace")
            return [
                {key: clean(value) for key, value in row.items()}
                for row in csv.DictReader(io.StringIO(text), delimiter="~")
            ]
        return table("products.txt"), table("patent.txt"), table("exclusivity.txt")


def orange_application(kind: str | None, number: str | None) -> str | None:
    number = clean(number)
    if not number:
        return None
    prefix = {"N": "NDA", "A": "ANDA", "B": "BLA"}.get((kind or "").upper(), kind or "APP")
    return f"{prefix}{number.zfill(6)}"


def chunks(rows: list[dict], size: int = 1000):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def cache_key(endpoint: str, application: str) -> Path:
    return DATA_DIR / f"openfda-{endpoint}-{application.lower()}.json"


def openfda_query(endpoint: str, field: str, application: str) -> tuple[list[dict], str]:
    params = {"search": f'{field}:"{application}"', "limit": OPENFDA_LIMIT}
    if API_KEY:
        params["api_key"] = API_KEY
    request = requests.Request("GET", f"{OPENFDA_ROOT}/{endpoint}.json", params=params).prepare()
    public_url = re.sub(r"([?&])api_key=[^&]+&?", r"\1", request.url or "").rstrip("?&")
    path = cache_key(endpoint, application)
    if path.exists():
        data = json.loads(path.read_text())
    else:
        response = requests.get(
            f"{OPENFDA_ROOT}/{endpoint}.json", params=params, timeout=90,
            headers={"User-Agent": "openfda-neo4j-demo/1.0"},
        )
        if response.status_code == 404:
            data = {"meta": {"results": {"total": 0}}, "results": []}
        else:
            response.raise_for_status()
            data = response.json()
        path.write_text(json.dumps(data, indent=2))
        time.sleep(0.25)
    total = data.get("meta", {}).get("results", {}).get("total", 0)
    results = data.get("results", [])
    if total > len(results):
        print(f"  {endpoint} {application}: loaded {len(results)}/{total} (bounded sample)")
    else:
        print(f"  {endpoint} {application}: loaded {len(results)}")
    return results, public_url


def section(record: dict, key: str, limit: int = 20_000) -> str | None:
    value = record.get(key)
    if isinstance(value, list):
        value = "\n".join(str(x) for x in value)
    value = clean(value)
    return value[:limit] if value else None


def run(session, query: str, **params):
    return session.run(query, **params).consume()


CONSTRAINTS = [
    "CREATE CONSTRAINT application_bla IF NOT EXISTS FOR (n:Application) REQUIRE n.bla IS UNIQUE",
    "CREATE CONSTRAINT application_number IF NOT EXISTS FOR (n:Application) REQUIRE n.application_number IS UNIQUE",
    "CREATE CONSTRAINT pb_product_id IF NOT EXISTS FOR (n:BiologicProduct) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT orange_product_id IF NOT EXISTS FOR (n:OrangeBookProduct) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT patent_number IF NOT EXISTS FOR (n:Patent) REQUIRE n.number IS UNIQUE",
    "CREATE CONSTRAINT exclusivity_id IF NOT EXISTS FOR (n:Exclusivity) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT openfda_product_id IF NOT EXISTS FOR (n:OpenFDAProduct) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT submission_id IF NOT EXISTS FOR (n:Submission) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT label_id IF NOT EXISTS FOR (n:Label) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT ndc_product_id IF NOT EXISTS FOR (n:NdcProduct) REQUIRE n.product_id IS UNIQUE",
    "CREATE CONSTRAINT package_ndc IF NOT EXISTS FOR (n:Package) REQUIRE n.package_ndc IS UNIQUE",
    "CREATE CONSTRAINT drug_name IF NOT EXISTS FOR (n:DrugName) REQUIRE n.normalized IS UNIQUE",
    "CREATE CONSTRAINT snapshot_id IF NOT EXISTS FOR (n:DatasetSnapshot) REQUIRE n.id IS UNIQUE",
]


def add_name(session, node_label: str, node_key: str, node_value: str,
             name: str | None, kind: str, source: str) -> None:
    normalized = normalize_name(name)
    if not normalized:
        return
    if node_label not in {"BiologicProduct", "OpenFDAProduct", "NdcProduct"}:
        raise ValueError("unexpected node label")
    query = f"""
    MATCH (entity:{node_label} {{{node_key}: $node_value}})
    MERGE (name:DrugName {{normalized: $normalized}})
      ON CREATE SET name.display = $display
    MERGE (entity)-[:HAS_NAME {{kind: $kind, source: $source}}]->(name)
    """
    run(session, query, node_value=node_value, normalized=normalized,
        display=clean(name), kind=kind, source=source)


def load_purple_book(session, rows: list[dict[str, str | None]], snapshot: dict) -> None:
    run(session, """
    MERGE (s:DatasetSnapshot {id: $id})
    SET s += $props
    """, id=snapshot["id"], props=snapshot)

    for row in rows:
        bla = normalize_bla(row.get("BLA Number"))
        product_number = row["Product Number"]
        product_id = f"{bla}:{product_number}"
        app_props = {
            "bla": bla,
            "application_number": bla,
            "purplebook_bla": clean(row.get("BLA Number")),
            "applicant": clean(row.get("Applicant")),
            "bla_type": clean(row.get("BLA Type")) or clean(row.get("License Type")),
            "center": clean(row.get("Center")),
            "license_number": clean(row.get("License Number")),
            "source": "FDA Purple Book",
        }
        product_props = {
            "id": product_id,
            "product_number": product_number,
            "proprietary_name": clean(row.get("Proprietary Name")),
            "proper_name": clean(row.get("Proper Name")),
            "strength": clean(row.get("Strength")),
            "dosage_form": clean(row.get("Dosage Form")),
            "route": clean(row.get("Route of Administration")),
            "presentation": clean(row.get("Product Presentation")),
            "marketing_status": clean(row.get("Marketing Status")),
            "licensure": clean(row.get("Licensure")),
            "approval_date": clean(row.get("Approval Date")),
            "supplement_number": clean(row.get("Supplement Number")),
            "submission_type": clean(row.get("Submission Type")),
            "reference_proper_name": clean(row.get("Ref. Product Proper Name")),
            "reference_proprietary_name": clean(row.get("Ref. Product Proprietary Name")),
            "source": "FDA Purple Book",
        }
        run(session, """
        MERGE (a:Application {bla: $bla}) SET a += $app
        MERGE (p:BiologicProduct {id: $product_id}) SET p += $product
        MERGE (a)-[:HAS_PRODUCT]->(p)
        WITH p
        MATCH (s:DatasetSnapshot {id: $snapshot_id})
        MERGE (s)-[:CONTAINS]->(p)
        """, bla=bla, app=app_props, product_id=product_id,
            product=product_props, snapshot_id=snapshot["id"])
        add_name(session, "BiologicProduct", "id", product_id,
                 row.get("Proprietary Name"), "brand", "Purple Book")
        add_name(session, "BiologicProduct", "id", product_id,
                 row.get("Proper Name"), "proper", "Purple Book")


def resolve_reference_edges(session, rows: list[dict[str, str | None]]) -> int:
    candidates: dict[str, set[str]] = {}
    for row in rows:
        license_type = clean(row.get("BLA Type")) or clean(row.get("License Type"))
        if not (license_type or "").startswith("351(a)"):
            continue
        bla = normalize_bla(row.get("BLA Number"))
        for field in ("Proper Name", "Proprietary Name"):
            if name := normalize_name(row.get(field)):
                candidates.setdefault(name, set()).add(bla)

    created = 0
    seen: set[tuple[str, str]] = set()
    for row in rows:
        source = normalize_bla(row.get("BLA Number"))
        refs = [normalize_name(row.get("Ref. Product Proper Name")),
                normalize_name(row.get("Ref. Product Proprietary Name"))]
        matches = set().union(*(candidates.get(x, set()) for x in refs if x))
        matches.discard(source)
        if len(matches) != 1:
            continue
        target = next(iter(matches))
        if (source, target) in seen:
            continue
        run(session, """
        MATCH (source:Application {bla: $source}), (target:Application {bla: $target})
        MERGE (source)-[r:REFERENCES]->(target)
        SET r.match_method = 'unique normalized Purple Book reference name',
            r.confidence = 'high', r.source = 'FDA Purple Book'
        """, source=source, target=target)
        seen.add((source, target))
        created += 1
    return created


def load_orange_book(session, products: list[dict], patents: list[dict],
                     exclusivities: list[dict], snapshot: dict) -> None:
    run(session, "MERGE (s:DatasetSnapshot {id: $id}) SET s += $props",
        id=snapshot["id"], props=snapshot)

    prepared = []
    for row in products:
        application = orange_application(row.get("Appl_Type"), row.get("Appl_No"))
        product_number = clean(row.get("Product_No"))
        if not application or not product_number:
            continue
        prepared.append({
            "application": application,
            "application_type": clean(row.get("Appl_Type")),
            "id": f"{application}:{product_number}",
            "product_number": product_number,
            "ingredient": clean(row.get("Ingredient")),
            "ingredient_normalized": normalize_name(row.get("Ingredient")),
            "trade_name": clean(row.get("Trade_Name")),
            "trade_normalized": normalize_name(row.get("Trade_Name")),
            "applicant": clean(row.get("Applicant")),
            "applicant_full_name": clean(row.get("Applicant_Full_Name")),
            "dosage_form_route": clean(row.get("DF;Route")),
            "strength": clean(row.get("Strength")),
            "te_code": clean(row.get("TE_Code")),
            "approval_date": clean(row.get("Approval_Date")),
            "rld": clean(row.get("RLD")),
            "rs": clean(row.get("RS")),
            "product_type": clean(row.get("Type")),
        })
    for batch in chunks(prepared):
        run(session, """
        UNWIND $rows AS row
        MERGE (a:Application {application_number: row.application})
        SET a:OrangeBookApplication, a.application_type = row.application_type,
            a.orange_book_applicant = row.applicant_full_name,
            a.orange_book_source = 'FDA Orange Book'
        MERGE (p:OrangeBookProduct {id: row.id})
        SET p += row, p.source = 'FDA Orange Book'
        MERGE (a)-[:HAS_ORANGE_BOOK_PRODUCT]->(p)
        WITH p, row
        MATCH (snapshot:DatasetSnapshot {id: $snapshot_id})
        MERGE (snapshot)-[:CONTAINS]->(p)
        FOREACH (_ IN CASE WHEN row.trade_normalized IS NULL THEN [] ELSE [1] END |
          MERGE (name:DrugName {normalized: row.trade_normalized})
          ON CREATE SET name.display = row.trade_name
          MERGE (p)-[:HAS_NAME {kind: 'brand', source: 'Orange Book'}]->(name))
        FOREACH (_ IN CASE WHEN row.ingredient_normalized IS NULL THEN [] ELSE [1] END |
          MERGE (name:DrugName {normalized: row.ingredient_normalized})
          ON CREATE SET name.display = row.ingredient
          MERGE (p)-[:HAS_NAME {kind: 'ingredient', source: 'Orange Book'}]->(name))
        """, rows=batch, snapshot_id=snapshot["id"])

    prepared_patents = []
    for row in patents:
        application = orange_application(row.get("Appl_Type"), row.get("Appl_No"))
        product_number = clean(row.get("Product_No"))
        number = clean(row.get("Patent_No"))
        if application and product_number and number:
            prepared_patents.append({
                "product_id": f"{application}:{product_number}", "number": number,
                "expiry": clean(row.get("Patent_Expire_Date_Text")),
                "drug_substance": clean(row.get("Drug_Substance_Flag")),
                "drug_product": clean(row.get("Drug_Product_Flag")),
                "use_code": clean(row.get("Patent_Use_Code")),
                "delist": clean(row.get("Delist_Flag")),
                "submission_date": clean(row.get("Submission_Date")),
            })
    for batch in chunks(prepared_patents):
        run(session, """
        UNWIND $rows AS row
        MATCH (p:OrangeBookProduct {id: row.product_id})
        MERGE (patent:Patent {number: row.number})
        SET patent.expiry = row.expiry, patent.source = 'FDA Orange Book'
        MERGE (p)-[r:HAS_PATENT]->(patent)
        SET r.drug_substance = row.drug_substance, r.drug_product = row.drug_product,
            r.use_code = row.use_code, r.delist = row.delist,
            r.submission_date = row.submission_date
        """, rows=batch)

    prepared_exclusivities = []
    for row in exclusivities:
        application = orange_application(row.get("Appl_Type"), row.get("Appl_No"))
        product_number = clean(row.get("Product_No"))
        code = clean(row.get("Exclusivity_Code"))
        expiry = clean(row.get("Exclusivity_Date"))
        if application and product_number and code:
            product_id = f"{application}:{product_number}"
            prepared_exclusivities.append({
                "product_id": product_id, "id": f"{product_id}:{code}:{expiry or ''}",
                "code": code, "expiry": expiry,
            })
    for batch in chunks(prepared_exclusivities):
        run(session, """
        UNWIND $rows AS row
        MATCH (p:OrangeBookProduct {id: row.product_id})
        MERGE (e:Exclusivity {id: row.id})
        SET e.code = row.code, e.expiry = row.expiry, e.source = 'FDA Orange Book'
        MERGE (p)-[:HAS_EXCLUSIVITY]->(e)
        """, rows=batch)


def load_drugsfda(session, application: str, records: list[dict], source_url: str) -> None:
    for record in records:
        run(session, """
        MATCH (a:Application {bla: $bla})
        SET a.drugsfda_sponsor = $sponsor, a.drugsfda_matched = true,
            a.drugsfda_source_url = $source_url
        """, bla=application, sponsor=clean(record.get("sponsor_name")), source_url=source_url)
        for product in record.get("products", []):
            number = clean(product.get("product_number")) or "unknown"
            product_id = f"{application}:{number}"
            props = {
                "id": product_id,
                "product_number": number,
                "brand_name": clean(product.get("brand_name")),
                "dosage_form": clean(product.get("dosage_form")),
                "route": clean(product.get("route")),
                "marketing_status": clean(product.get("marketing_status")),
                "reference_drug": clean(product.get("reference_drug")),
                "active_ingredients": scalar_list(
                    x.get("name") for x in product.get("active_ingredients", [])
                ),
                "ingredient_strengths": scalar_list(
                    x.get("strength") for x in product.get("active_ingredients", [])
                ),
                "source_url": source_url,
            }
            run(session, """
            MATCH (a:Application {bla: $bla})
            MERGE (p:OpenFDAProduct {id: $id}) SET p += $props
            MERGE (a)-[:HAS_OPENFDA_PRODUCT]->(p)
            WITH a, p
            OPTIONAL MATCH (a)-[:HAS_PRODUCT]->(pb:BiologicProduct {product_number: $number})
            FOREACH (_ IN CASE WHEN pb IS NULL THEN [] ELSE [1] END |
              MERGE (pb)-[:SAME_APPLICATION_PRODUCT_NUMBER]->(p))
            """, bla=application, id=product_id, props=props, number=number)
            add_name(session, "OpenFDAProduct", "id", product_id,
                     product.get("brand_name"), "brand", "Drugs@FDA")
            for ingredient in product.get("active_ingredients", []):
                add_name(session, "OpenFDAProduct", "id", product_id,
                         ingredient.get("name"), "ingredient", "Drugs@FDA")
        for submission in record.get("submissions", []):
            number = clean(submission.get("submission_number")) or "unknown"
            kind = clean(submission.get("submission_type")) or "unknown"
            submission_id = f"{application}:{kind}:{number}"
            props = {
                "id": submission_id,
                "type": kind,
                "number": number,
                "status": clean(submission.get("submission_status")),
                "status_date": clean(submission.get("submission_status_date")),
                "review_priority": clean(submission.get("review_priority")),
                "class_code": clean(submission.get("submission_class_code")),
                "class_description": clean(submission.get("submission_class_code_description")),
                "document_urls": scalar_list(
                    x.get("url") for x in submission.get("application_docs", [])
                ),
                "source_url": source_url,
            }
            run(session, """
            MATCH (a:Application {bla: $bla})
            MERGE (s:Submission {id: $id}) SET s += $props
            MERGE (a)-[:HAS_SUBMISSION]->(s)
            """, bla=application, id=submission_id, props=props)


def load_labels(session, application: str, records: list[dict], source_url: str) -> None:
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for record in records:
        label_id = clean(record.get("id"))
        if not label_id:
            continue
        openfda = record.get("openfda") or {}
        props = {
            "id": label_id,
            "set_id": clean(record.get("set_id")),
            "version": clean(record.get("version")),
            "effective_time": clean(record.get("effective_time")),
            "brand_names": scalar_list(openfda.get("brand_name", [])),
            "generic_names": scalar_list(openfda.get("generic_name", [])),
            "manufacturers": scalar_list(openfda.get("manufacturer_name", [])),
            "routes": scalar_list(openfda.get("route", [])),
            "indications_and_usage": section(record, "indications_and_usage"),
            "boxed_warning": section(record, "boxed_warning"),
            "warnings": section(record, "warnings"),
            "warnings_and_cautions": section(record, "warnings_and_cautions"),
            "contraindications": section(record, "contraindications"),
            "dosage_and_administration": section(record, "dosage_and_administration"),
            "dosage_forms_and_strengths": section(record, "dosage_forms_and_strengths"),
            "inactive_ingredient": section(record, "inactive_ingredient"),
            "storage_and_handling": section(record, "storage_and_handling"),
            "how_supplied": section(record, "how_supplied"),
            "description": section(record, "description"),
            "source_url": source_url,
            "last_seen_at": observed_at,
        }
        run(session, """
        MATCH (a:Application {bla: $bla})
        MERGE (l:Label {id: $id})
        SET l += $props,
            l.first_seen_at = coalesce(l.first_seen_at, $observed_at)
        MERGE (a)-[:HAS_LABEL]->(l)
        """, bla=application, id=label_id, props=props, observed_at=observed_at)


def load_ndc(session, application: str, records: list[dict], source_url: str) -> None:
    for record in records:
        product_id = clean(record.get("product_id")) or clean(record.get("product_ndc"))
        if not product_id:
            continue
        props = {
            "product_id": product_id,
            "product_ndc": clean(record.get("product_ndc")),
            "brand_name": clean(record.get("brand_name")),
            "generic_name": clean(record.get("generic_name")),
            "labeler_name": clean(record.get("labeler_name")),
            "dosage_form": clean(record.get("dosage_form")),
            "routes": scalar_list(record.get("route", [])),
            "marketing_category": clean(record.get("marketing_category")),
            "marketing_start_date": clean(record.get("marketing_start_date")),
            "marketing_end_date": clean(record.get("marketing_end_date")),
            "active_ingredients": scalar_list(
                x.get("name") for x in record.get("active_ingredients", [])
            ),
            "source_url": source_url,
        }
        run(session, """
        MATCH (a:Application {bla: $bla})
        MERGE (n:NdcProduct {product_id: $id}) SET n += $props
        MERGE (a)-[:HAS_NDC_LISTING]->(n)
        """, bla=application, id=product_id, props=props)
        add_name(session, "NdcProduct", "product_id", product_id,
                 record.get("brand_name"), "brand", "NDC Directory")
        add_name(session, "NdcProduct", "product_id", product_id,
                 record.get("generic_name"), "generic", "NDC Directory")
        for package in record.get("packaging", []):
            package_ndc = clean(package.get("package_ndc"))
            if not package_ndc:
                continue
            run(session, """
            MATCH (n:NdcProduct {product_id: $product_id})
            MERGE (p:Package {package_ndc: $package_ndc})
            SET p.description = $description, p.sample = $sample,
                p.source_url = $source_url
            MERGE (n)-[:HAS_PACKAGE]->(p)
            """, product_id=product_id, package_ndc=package_ndc,
                description=clean(package.get("description")),
                sample=bool(package.get("sample")), source_url=source_url)


def main() -> None:
    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if PURPLEBOOK_URL.lower() == "latest":
        purplebook_url, payload = latest_purple_book()
    else:
        purplebook_url = PURPLEBOOK_URL
        payload = fetch_bytes(
            purplebook_url,
            f"purplebook-{hashlib.sha256(purplebook_url.encode()).hexdigest()[:12]}.csv",
        )
    title, all_rows = parse_purple_book(payload)
    rows = select_cohort(all_rows)
    applications = sorted({normalize_bla(r.get("BLA Number")) for r in rows})
    applications = [x for x in applications if x]
    print(f"Purple Book: {len(all_rows)} full-snapshot rows; selected "
          f"{len(rows)} products in {len(applications)} applications")

    digest = hashlib.sha256(payload).hexdigest()
    snapshot = {
        "id": f"purplebook:{digest[:16]}",
        "dataset": "FDA Purple Book",
        "title": title,
        "source_url": purplebook_url,
        "retrieved_at": retrieved_at,
        "sha256": digest,
        "complete_source_snapshot": False,
        "source_snapshot_rows": len(all_rows),
        "loaded_rows": len(rows),
        "sampled": True,
        "cohort_terms": SEED_TERMS,
    }

    orange_payload = fetch_bytes(ORANGEBOOK_URL, "orangebook-latest.zip", refresh=True)
    orange_products, orange_patents, orange_exclusivities = parse_orange_book(orange_payload)
    orange_digest = hashlib.sha256(orange_payload).hexdigest()
    orange_snapshot = {
        "id": f"orangebook:{orange_digest[:16]}",
        "dataset": "FDA Orange Book",
        "title": "Current Orange Book data files",
        "source_url": ORANGEBOOK_URL,
        "retrieved_at": retrieved_at,
        "sha256": orange_digest,
        "complete_source_snapshot": True,
    }
    print(f"Orange Book: {len(orange_products)} products, {len(orange_patents)} patents, "
          f"{len(orange_exclusivities)} exclusivities")

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USERNAME", "neo4j"),
              os.getenv("NEO4J_PASSWORD", "openfda-demo")),
    )
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            for statement in CONSTRAINTS:
                run(session, statement)
            load_purple_book(session, rows, snapshot)
            reference_edges = resolve_reference_edges(session, rows)
            load_orange_book(
                session, orange_products, orange_patents, orange_exclusivities,
                orange_snapshot,
            )
            for application in applications:
                print(f"enriching {application}")
                records, url = openfda_query("drugsfda", "application_number", application)
                load_drugsfda(session, application, records, url)
                records, url = openfda_query("label", "openfda.application_number", application)
                load_labels(session, application, records, url)
                records, url = openfda_query("ndc", "application_number", application)
                load_ndc(session, application, records, url)

            counts = session.run("""
            MATCH (n)
            RETURN labels(n)[0] AS label, count(*) AS count
            ORDER BY count DESC
            """).data()
        print(f"created/resolved {reference_edges} reference-application edges")
        print("graph counts:")
        for row in counts:
            print(f"  {row['label']}: {row['count']}")
        print("Open http://localhost:7474 and run queries from queries.cypher")
    finally:
        driver.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"load failed: {exc}", file=sys.stderr)
        raise
