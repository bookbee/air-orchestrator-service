#!/usr/bin/env python3
"""Requirement -> test traceability check.

Constitution Article X: a claim is proved by a test, not by assertion. This script
is what makes that enforceable rather than aspirational.

For every `docs/specs/NNNN-*/spec.md` it reads two tables — the requirements in the
section titled "Requirements", and the mapping in the section titled "Traceability"
— and checks that they agree with each other and with the test suite as it actually
exists on disk.

It fails the build on a *broken link*:

  * a requirement that is declared but never traced, or traced but never declared;
  * a requirement traced twice;
  * a named test that does not exist (the common case: a test was renamed).

It does **not** fail on a declared `GAP` or `DEFERRED`. Those are tracked debt, and
forcing them to be written down is the point — a spec that must name its gaps cannot
quietly have them. They are reported on every run so they stay visible.

Tests are indexed by parsing the files with `ast` rather than by asking pytest,
because pytest's collection output format is not a stable interface and this check
must keep working across pytest versions.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = ROOT / "docs" / "specs"
TESTS_DIR = ROOT / "tests"

#: `REQ-<AREA>-<NNN>`. The template's literal `REQ-<AREA>-001` cannot match, so
#: templates are skipped for free — but they are excluded explicitly too, because
#: relying on that would be a trap for whoever edits the template next.
REQ_RE = re.compile(r"REQ-[A-Z][A-Z0-9]*-\d{3}")
#: A pytest node id: a path ending in `.py`, `::`, then the test name.
NODE_RE = re.compile(r"(tests/[\w/]+\.py)::([\w:]+)")
#: Markers that declare an absence rather than naming a proof.
GAP_RE = re.compile(r"\b(GAP|DEFERRED|WITHDRAWN)\b")

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)


@dataclass
class SpecReport:
    """One spec's results."""

    name: str
    proved: dict[str, list[str]] = field(default_factory=dict)
    gaps: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.proved) + len(self.gaps)


def index_tests(tests_dir: Path) -> set[str]:
    """Every test node id in the suite, as `path::name` (and `path::Class::name`).

    Parsed rather than collected: a syntax error in a test file is reported here as
    an unreadable file instead of crashing the run, and the result does not depend
    on the pytest version.
    """
    found: set[str] = set()
    for path in sorted(tests_dir.rglob("test_*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:  # pragma: no cover - reported, not raised
            print(f"{RED}!{RESET} could not parse {rel}: {exc}", file=sys.stderr)
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                found.add(f"{rel}::{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for item in node.body:
                    if isinstance(
                        item, ast.FunctionDef | ast.AsyncFunctionDef
                    ) and item.name.startswith("test"):
                        found.add(f"{rel}::{node.name}::{item.name}")
    return found


def section(text: str, title_contains: str) -> str:
    """The body of the first `##` section whose title contains `title_contains`.

    Case-insensitive, and tolerant of the numbering (`## 5. Requirements`) so a spec
    can renumber its sections without breaking the check.
    """
    needle = title_contains.lower()
    chunks = re.split(r"^##\s+", text, flags=re.MULTILINE)
    for chunk in chunks[1:]:
        heading, _, body = chunk.partition("\n")
        if needle in heading.lower():
            return body
    return ""


def table_rows(body: str) -> list[list[str]]:
    """Pipe-table rows, minus the header and the `---` separator."""
    rows: list[list[str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or all(set(cell) <= {"-", ":", " "} for cell in cells if cell):
            continue
        rows.append(cells)
    return rows


def check_spec(path: Path, known_tests: set[str]) -> SpecReport:
    text = path.read_text(encoding="utf-8")
    report = SpecReport(name=path.parent.name)

    declared: list[str] = []
    for cells in table_rows(section(text, "requirements")):
        ids = REQ_RE.findall(cells[0])
        declared.extend(ids)

    trace_body = section(text, "traceability")
    if not trace_body.strip():
        report.errors.append("no Traceability section")
        return report

    seen: set[str] = set()
    for cells in table_rows(trace_body):
        ids = REQ_RE.findall(cells[0])
        if not ids:
            continue
        req = ids[0]
        proof = " ".join(cells[1:])
        if req in seen:
            report.errors.append(f"{req} traced more than once")
            continue
        seen.add(req)

        # The marker is checked *before* the node ids, and deliberately so. A gap
        # row often cites a real test to explain what it does not cover, and a
        # deferred row should name the test it intends to add — deciding the test
        # name before writing the code is the practice, not an accident. Reading
        # either as a proof would let a spec launder a gap into coverage.
        marker = GAP_RE.search(proof)
        nodes = [f"{p}::{n}" for p, n in NODE_RE.findall(proof)]
        if marker is not None:
            report.gaps[req] = marker.group(1)
        elif nodes:
            missing = [node for node in nodes if node not in known_tests]
            if missing:
                for node in missing:
                    report.errors.append(f"{req} names a test that does not exist: {node}")
            else:
                report.proved[req] = nodes
        else:
            report.errors.append(
                f"{req} neither names a test nor declares GAP/DEFERRED (Article X)"
            )

    for req in declared:
        if req not in seen:
            report.errors.append(f"{req} is declared in Requirements but never traced")
    for req in sorted(seen):
        if req not in declared:
            report.errors.append(f"{req} is traced but not declared in Requirements")

    return report


def main() -> int:
    if not SPECS_DIR.is_dir():
        print(f"{RED}no docs/specs directory{RESET}", file=sys.stderr)
        return 1

    known_tests = index_tests(TESTS_DIR)
    spec_paths = sorted(p for p in SPECS_DIR.glob("*/spec.md") if p.parent.name[0].isdigit())
    if not spec_paths:
        print(f"{YELLOW}no specs yet{RESET} — copy docs/specs/templates/spec.md to start")
        return 0

    reports = [check_spec(path, known_tests) for path in spec_paths]

    print(f"{BOLD}traceability{RESET} {DIM}({len(known_tests)} tests indexed){RESET}\n")
    total_proved = total_gaps = total_errors = 0
    for report in reports:
        mark = RED + "FAIL" + RESET if report.errors else GREEN + " ok " + RESET
        print(
            f"  [{mark}] {report.name:<28} "
            f"{len(report.proved)}/{report.total} proved"
            + (f"{YELLOW}  {len(report.gaps)} gap(s){RESET}" if report.gaps else "")
        )
        for req, marker in sorted(report.gaps.items()):
            print(f"         {YELLOW}{marker:<9}{RESET} {DIM}{req}{RESET}")
        for error in report.errors:
            print(f"         {RED}error{RESET}     {error}")
        total_proved += len(report.proved)
        total_gaps += len(report.gaps)
        total_errors += len(report.errors)

    print(
        f"\n  {total_proved} proved · {total_gaps} declared gap(s) · "
        f"{total_errors} error(s) across {len(reports)} spec(s)"
    )
    if total_errors:
        print(
            f"\n{RED}traceability is broken{RESET} — a requirement points at a test "
            f"that does not exist, or is untraced."
        )
        return 1
    if total_gaps:
        print(
            f"\n{GREEN}links intact{RESET}; {total_gaps} declared gap(s) remain — tracked, not hidden."
        )
    else:
        print(f"\n{GREEN}links intact{RESET}; every requirement is proved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
