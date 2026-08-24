#!/usr/bin/env python3
"""Generate Graphviz schema diagrams from the live Neo4j FDA graph."""

from __future__ import annotations

import argparse
import html
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "docs" / "fda-graph-schema"


def type_id(labels: list[str]) -> str:
    return "__".join(sorted(labels))


def display_type(labels: list[str]) -> str:
    return ":".join(sorted(labels))


def color_for(labels: list[str]) -> str:
    joined = " ".join(labels)
    if "OrangeBook" in joined or any(x in joined for x in ("Patent", "Exclusivity")):
        return "#ffedd5"
    if any(x in joined for x in ("Biologic", "Submission")):
        return "#e0e7ff"
    if any(x in joined for x in ("Label", "Ndc", "OpenFDA", "Package")):
        return "#dcfce7"
    if "DrugName" in joined:
        return "#f3e8ff"
    if "DatasetSnapshot" in joined:
        return "#e5e7eb"
    return "#fee2e2"


def q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def load_schema(uri: str, username: str, password: str):
    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            nodes = [dict(row) for row in session.run("""
                MATCH (n)
                RETURN labels(n) AS labels, count(*) AS count
                ORDER BY labels
            """)]
            properties = [dict(row) for row in session.run("""
                MATCH (n)
                UNWIND keys(n) AS property
                RETURN labels(n) AS labels, property, count(*) AS populated
                ORDER BY labels, property
            """)]
            relationships = [dict(row) for row in session.run("""
                MATCH (source)-[relationship]->(target)
                RETURN labels(source) AS source_labels,
                       type(relationship) AS relationship,
                       labels(target) AS target_labels,
                       count(*) AS count
                ORDER BY source_labels, relationship, target_labels
            """)]
    finally:
        driver.close()
    return nodes, properties, relationships


def build_dot(nodes: list[dict], properties: list[dict], relationships: list[dict]) -> str:
    props: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in properties:
        props[type_id(row["labels"])].append((row["property"], row["populated"]))

    lines = [
        "digraph FDA_GRAPH_SCHEMA {",
        "  graph [rankdir=LR, bgcolor=\"#f8fafc\", pad=0.35, nodesep=0.55, ranksep=1.1,",
        "         fontname=\"Helvetica\", label=\"DrugGraph — Live FDA Schema\",",
        "         labelloc=t, fontsize=24, fontcolor=\"#17201c\", splines=polyline];",
        "  node [shape=plain, fontname=\"Helvetica\"];",
        "  edge [fontname=\"Helvetica\", fontsize=9, color=\"#64748b\", fontcolor=\"#334155\", arrowsize=0.75];",
    ]

    for row in nodes:
        labels = row["labels"]
        node_id = type_id(labels)
        title = html.escape(display_type(labels))
        rows = [
            f'<TR><TD BGCOLOR="{color_for(labels)}" ALIGN="LEFT" CELLPADDING="8">'
            f'<FONT POINT-SIZE="15"><B>:{title}</B></FONT><BR/>'
            f'<FONT POINT-SIZE="10">{row["count"]:,} nodes</FONT></TD></TR>'
        ]
        for prop, populated in props[node_id]:
            rows.append(
                '<TR><TD ALIGN="LEFT" BGCOLOR="#ffffff" CELLPADDING="4">'
                f'<FONT FACE="Courier" POINT-SIZE="9">{html.escape(prop)}</FONT>'
                f'<FONT COLOR="#64748b" POINT-SIZE="8">  {populated:,}</FONT></TD></TR>'
            )
        table = (
            '<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" '
            'COLOR="#cbd5e1" STYLE="ROUNDED">' + "".join(rows) + "</TABLE>>"
        )
        lines.append(f"  {q(node_id)} [label={table}];")

    for row in relationships:
        source = type_id(row["source_labels"])
        target = type_id(row["target_labels"])
        label = f'{row["relationship"]}  ({row["count"]:,})'
        lines.append(f"  {q(source)} -> {q(target)} [label={q(label)}];")

    lines.extend([
        '  subgraph cluster_legend {',
        '    label="Color key"; fontsize=11; color="#cbd5e1"; style="rounded,dashed";',
        '    legend [shape=plain, label=<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" COLOR="#cbd5e1">',
        '      <TR><TD BGCOLOR="#ffedd5">Orange Book</TD><TD BGCOLOR="#e0e7ff">Purple Book</TD>',
        '          <TD BGCOLOR="#dcfce7">openFDA / labels / NDC</TD><TD BGCOLOR="#f3e8ff">Shared identity</TD></TR>',
        '    </TABLE>>];',
        '  }',
        "}",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="output path without extension")
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--username", default=os.getenv("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", "openfda-demo"))
    args = parser.parse_args()

    nodes, properties, relationships = load_schema(args.uri, args.username, args.password)
    dot = build_dot(nodes, properties, relationships)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dot_path = args.output.with_suffix(".dot")
    svg_path = args.output.with_suffix(".svg")
    png_path = args.output.with_suffix(".png")
    dot_path.write_text(dot)
    subprocess.run(["dot", "-Tsvg", str(dot_path), "-o", str(svg_path)], check=True)
    subprocess.run(["dot", "-Tpng", "-Gdpi=180", str(dot_path), "-o", str(png_path)], check=True)
    print(f"Generated {svg_path}")
    print(f"Generated {png_path}")
    print(f"Schema: {len(nodes)} node types, {len(relationships)} relationship patterns")


if __name__ == "__main__":
    main()
