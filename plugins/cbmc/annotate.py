"""Insert CBMC contracts into annotated copies of the project's C sources.

For every C source file listed in ``phases.json``, the original is copied to
``fm_agent/cbmc/annotated/<same relative path>`` with each function's validated
``.cbmc.json`` contract rendered as ``__CPROVER_requires`` / ``__CPROVER_ensures``
/ ``__CPROVER_assigns`` clauses between its declarator and body, and its
``unexpressible`` entries as a comment block above it.  The project sources are
never modified.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from plugins.cbmc.contracts import CONTRACT_SUFFIX, _load_valid_contract
from src.extract import EXT_TO_LANG, _function_spans
from src.file_utils import _is_test_file
from src.incremental_reasoner import _src_rel_to_func_dir

ANNOTATED_DIR = "annotated"
_CLAUSE_INDENT = "    "
# "__CPROVER_forall { int i; ..." — captures the bound name and the opening brace.
_QUANTIFIER = re.compile(
    r"__CPROVER_(?:forall|exists)\s*(?P<brace>\{)\s*[^;{}]*?\b(?P<name>[A-Za-z_]\w*)\s*;"
)


def _body_brace(lines: list[str], start: int, end: int) -> tuple[int, int] | None:
    """``(line_idx, col)`` of the ``{`` opening the function body, or None.

    That is the first ``{`` at paren depth 0 after the parameter list closes;
    comments and string/char literals are skipped.
    """
    depth = 0
    seen_params = False
    in_block_comment = False
    for idx in range(start, min(end, len(lines) - 1) + 1):
        line = lines[idx]
        in_str = None
        i = 0
        while i < len(line):
            ch = line[i]
            if in_block_comment:
                if line.startswith("*/", i):
                    in_block_comment = False
                    i += 2
                    continue
            elif in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif line.startswith("/*", i):
                in_block_comment = True
                i += 2
                continue
            elif line.startswith("//", i):
                break
            elif ch in ("'", '"'):
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    seen_params = True
            elif ch == "{" and depth == 0 and seen_params:
                return idx, i
            i += 1
    return None


def _line_ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")):] or "\n"


def _quantifier_body_end(expr: str, open_idx: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``open_idx``."""
    depth = 0
    for idx in range(open_idx, len(expr)):
        if expr[idx] == "{":
            depth += 1
        elif expr[idx] == "}":
            depth -= 1
            if depth == 0:
                return idx + 1
    return len(expr)


def _rename_bound_vars(expr: str, used: set[str]) -> str:
    """Give each quantifier's bound variable a name not in ``used`` (updated in place).

    CBMC scopes every requires (and every ensures) clause of a function together,
    so two clauses that each bind ``int i`` are a redeclaration error once they sit
    on the same function, although each is valid on its own.
    """
    pos = 0
    while True:
        match = _QUANTIFIER.search(expr, pos)
        if match is None:
            return expr
        name = match.group("name")
        if name in used:
            suffix = 1
            while f"{name}_{suffix}" in used:
                suffix += 1
            fresh = f"{name}_{suffix}"
            start = match.start("brace")
            end = _quantifier_body_end(expr, start)
            body = re.sub(rf"\b{re.escape(name)}\b", fresh, expr[start:end])
            expr = expr[:start] + body + expr[end:]
            name = fresh
        used.add(name)
        pos = match.start("brace") + 1


def _render_clauses(contract: dict, newline: str) -> list[str]:
    used: set[str] = set()

    def exprs(key: str) -> list[str]:
        return [_rename_bound_vars(expr.strip(), used) for expr in contract[key]]

    clauses = [f"__CPROVER_requires({expr})" for expr in exprs("requires")]
    clauses += [f"__CPROVER_ensures({expr})" for expr in exprs("ensures")]
    # One clause per target: a "cond: target" entry guards every target after it
    # in the same clause, so joining entries with commas would change meaning.
    clauses += [f"__CPROVER_assigns({expr.strip()})" for expr in contract["assigns"]]
    if not contract["assigns"]:
        clauses.append("__CPROVER_assigns()")
    return [_CLAUSE_INDENT + clause + newline for clause in clauses]


def _render_unexpressible(contract: dict, newline: str) -> list[str]:
    if not contract["unexpressible"]:
        return []
    out = ["/* CBMC contract: not expressible" + newline]
    for item in contract["unexpressible"]:
        text = f"{item['spec_fragment'].strip()}: {item['reason'].strip()}"
        text = " ".join(text.split()).replace("*/", "* /").replace("/*", "/ *")
        out.append(f" * - {text}{newline}")
    out.append(" */" + newline)
    return out


def _annotate_lines(lines: list[str], start: int, brace: tuple[int, int],
                    contract: dict) -> list[str]:
    """Return ``lines`` with ``contract`` inserted around the span starting at ``start``."""
    brace_idx, brace_col = brace
    brace_line = lines[brace_idx]
    newline = _line_ending(brace_line)
    clauses = _render_clauses(contract, newline)
    before = brace_line[:brace_col].rstrip()
    if before.strip():
        replacement = [before + newline, *clauses, brace_line[brace_col:]]
    else:
        replacement = [*clauses, brace_line]
    lines = lines[:brace_idx] + replacement + lines[brace_idx + 1:]
    comment = _render_unexpressible(contract, _line_ending(lines[start]))
    return lines[:start] + comment + lines[start:]


def _c_source_files(phases_data: dict) -> list[str]:
    files = []
    for phase in phases_data.get("phases", []):
        for module in phase.get("modules", []):
            for src_rel in module.get("source_files", []):
                ext = src_rel.rsplit(".", 1)[-1].lower() if "." in src_rel else ""
                if EXT_TO_LANG.get(ext) == "c" and not _is_test_file(src_rel):
                    files.append(src_rel)
    return list(dict.fromkeys(files))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as file:
        file.write(text)
    os.replace(tmp, path)


def annotate_sources(proj_dir: str) -> dict:
    """Write contract-annotated copies of every C source; return stats for summary.json."""
    work_dir = Path(proj_dir) / "fm_agent"
    extracted_dir = work_dir / "extracted_functions"
    contract_dir = work_dir / "cbmc"
    out_dir = contract_dir / ANNOTATED_DIR
    with (work_dir / "phases.json").open("r", encoding="utf-8") as file:
        phases_data = json.load(file)

    files: list[str] = []
    annotated: list[str] = []
    missing: list[dict] = []
    no_body: list[str] = []
    for src_rel in _c_source_files(phases_data):
        abs_src = os.path.join(proj_dir, src_rel)
        if not os.path.isfile(abs_src):
            logging.warning("cbmc plugin: source file not found: %s", abs_src)
            continue
        spans, lines, _backend_available = _function_spans(abs_src, "c", proj_dir)
        func_dir, ext = _src_rel_to_func_dir(proj_dir, abs_src)
        rel_dir = Path(os.path.relpath(func_dir, extracted_dir))

        # Bottom-up, so each insertion leaves the earlier spans' indices valid.
        for ident, start, end in sorted(spans, key=lambda span: span[1], reverse=True):
            unit_rel = (rel_dir / f"{ident}.{ext}").as_posix()
            contract = _load_valid_contract(contract_dir / (unit_rel + CONTRACT_SUFFIX), unit_rel)
            if contract is None:
                missing.append({"unit": unit_rel, "reason": "no_valid_contract"})
                continue
            brace = _body_brace(lines, start, end)
            if brace is None:
                no_body.append(unit_rel)
                logging.warning("cbmc plugin: no function body found for %s", unit_rel)
                continue
            lines = _annotate_lines(lines, start, brace, contract)
            annotated.append(unit_rel)

        _write_text_atomic(out_dir / src_rel, "".join(lines))
        files.append(Path(src_rel).as_posix())

    return {
        "output_dir": f"fm_agent/cbmc/{ANNOTATED_DIR}",
        "files": sorted(files),
        "functions_annotated": sorted(annotated),
        "missing": sorted(missing, key=lambda item: item["unit"]),
        "no_body": sorted(no_body),
    }
