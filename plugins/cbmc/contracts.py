"""Generate CBMC function-contract JSON objects from FM-Agent NL specs.

For every extracted C function whose ``.spec.json`` is ready, one structured LLM
call turns the spec (plus the function source and callee expectations) into a
JSON object of ``__CPROVER_requires`` / ``__CPROVER_ensures`` /
``__CPROVER_assigns`` clause expressions.  The result is written to
``fm_agent/cbmc/<same relative path as the extracted file>.cbmc.json``.

Nothing is inserted into source files and ``cbmc`` is never executed; the
objects are the deliverable.  The hook is safe to repeat: a sidecar that already
validates is reused, so a ``--resume`` run makes no LLM call for it.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shutil
from pathlib import Path

from config import LLM_MODEL, MAX_WORKERS
from src.file_utils import _get_all_phase_files, _path_language
from src.llm_client import _llm_json_call, _llm_provider_client
from src.opencode_trace import function_id_from_extracted_path
from src.specification import SOFTWARE_PROFILE
from src.trace_writer import utc_now_iso

CONTRACT_SUFFIX = ".cbmc.json"
SYSTEM_PROMPT_NAME = "cbmc_contract_system_prompt.md"

_REQUIRED_KEYS = ("function", "signature", "requires", "ensures", "assigns", "unexpressible")
_CLAUSE_LISTS = ("requires", "ensures", "assigns")
_CLAUSE_WRAPPERS = ("__CPROVER_requires", "__CPROVER_ensures", "__CPROVER_assigns")
_ENSURES_ONLY = ("__CPROVER_return_value", "__CPROVER_old(")
_BRACKETS = {")": "(", "]": "[", "}": "{"}

SCHEMA_DESCRIPTION = (
    '{"function": "<name>", "signature": "<C prototype>", '
    '"requires": ["<C expression>", ...], "ensures": ["<C expression>", ...], '
    '"assigns": ["<lvalue or __CPROVER_object_* target>", ...], '
    '"unexpressible": [{"spec_fragment": "...", "reason": "..."}, ...]}'
)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _balanced(text: str) -> bool:
    stack: list[str] = []
    in_str = None
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in _BRACKETS:
            if not stack or stack[-1] != _BRACKETS[ch]:
                return False
            stack.pop()
        i += 1
    return not stack and in_str is None


def _expected_names(unit_rel: str) -> set[str]:
    """Names the ``function`` field may carry for one extracted file.

    ``foo.c`` -> ``foo``; a dedup suffix (``foo_1.c``) is accepted either way.
    """
    stem = Path(unit_rel).stem
    names = {stem}
    base = re.sub(r"_\d+$", "", stem)
    names.add(base)
    return names


def validate_contract(data: object, unit_rel: str) -> dict:
    """Return ``data`` when it is a well-formed contract object, else raise ValueError."""
    if not isinstance(data, dict):
        raise ValueError("contract must be a JSON object")
    unknown = sorted(set(data) - set(_REQUIRED_KEYS))
    if unknown:
        raise ValueError(f"unexpected keys: {', '.join(unknown)}")
    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise ValueError(f"missing keys: {', '.join(missing)}")

    for key in ("function", "signature"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"'{key}' must be a non-empty string")
    if data["function"].strip() not in _expected_names(unit_rel):
        raise ValueError(
            f"'function' must be {Path(unit_rel).stem!r}, got {data['function']!r}"
        )

    for key in _CLAUSE_LISTS:
        clauses = data[key]
        if not isinstance(clauses, list):
            raise ValueError(f"'{key}' must be a list")
        for clause in clauses:
            if not isinstance(clause, str) or not clause.strip():
                raise ValueError(f"'{key}' entries must be non-empty strings")
            text = clause.strip()
            if text.endswith(";"):
                raise ValueError(f"'{key}' clause must not end with ';': {text}")
            if "//" in text or "/*" in text:
                raise ValueError(f"'{key}' clause must not contain comments: {text}")
            if any(text.startswith(wrapper) for wrapper in _CLAUSE_WRAPPERS):
                raise ValueError(
                    f"'{key}' clause must be the bare expression, not wrapped in "
                    f"__CPROVER_*(...): {text}"
                )
            if not _balanced(text):
                raise ValueError(f"'{key}' clause has unbalanced brackets or quotes: {text}")
            if key != "ensures":
                for token in _ENSURES_ONLY:
                    if token in text:
                        raise ValueError(
                            f"{token.rstrip('(')} is only allowed in 'ensures': {text}"
                        )

    unexpressible = data["unexpressible"]
    if not isinstance(unexpressible, list):
        raise ValueError("'unexpressible' must be a list")
    for item in unexpressible:
        if not isinstance(item, dict) or set(item) != {"spec_fragment", "reason"}:
            raise ValueError(
                "'unexpressible' entries must be objects with exactly "
                "'spec_fragment' and 'reason'"
            )
        for key in ("spec_fragment", "reason"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ValueError(f"'unexpressible.{key}' must be a non-empty string")

    if not data["requires"] and not data["ensures"] and not unexpressible:
        raise ValueError(
            "contract is empty: give at least one requires/ensures clause, or "
            "record why the spec is unexpressible"
        )
    return data


def _load_valid_contract(path: Path, unit_rel: str) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return validate_contract(data, unit_rel)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #

def _read_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _stage_system_prompt(work_dir: Path) -> str:
    """Copy the system prompt into fm_agent/ so a run's trace is self-contained."""
    source = Path(__file__).with_name("prompts") / SYSTEM_PROMPT_NAME
    staged = work_dir / SYSTEM_PROMPT_NAME
    if source.resolve() != staged.resolve():
        shutil.copy2(source, staged)
    return staged.read_text(encoding="utf-8")


def _build_messages(system_prompt: str, unit_path: Path, unit_rel: str) -> list[dict]:
    paths = SOFTWARE_PROFILE.artifact_paths(unit_path)
    spec = _read_json(paths.self_spec) or {}
    info = _read_json(paths.dependency_info) or {"callees": []}
    source = unit_path.read_text(encoding="utf-8", errors="replace")
    user = (
        f"Function name: {Path(unit_rel).stem}\n"
        f"Extracted from: {unit_rel}\n\n"
        "Natural-language specification (.spec.json):\n"
        f"```json\n{json.dumps(spec, indent=2, ensure_ascii=False)}\n```\n\n"
        "Expectations this function places on its callees (.info.json):\n"
        f"```json\n{json.dumps(info, indent=2, ensure_ascii=False)}\n```\n\n"
        "Function source:\n"
        f"```c\n{source}\n```\n\n"
        "Translate the specification into a CBMC function contract. Return only a "
        f"valid JSON object with exactly this shape: {SCHEMA_DESCRIPTION}. "
        "Do not include Markdown, tags, or prose outside the JSON object."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ]


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")
    os.replace(tmp, path)


def _generate_one(system_prompt: str, input_dir: Path, unit_rel: str,
                  out_path: Path, trace_dir: Path) -> dict:
    unit_path = input_dir / unit_rel
    messages = _build_messages(system_prompt, unit_path, unit_rel)
    result = _llm_json_call(
        _llm_provider_client,
        LLM_MODEL,
        messages,
        lambda data: validate_contract(data, unit_rel),
        SCHEMA_DESCRIPTION,
        trace_dir=str(trace_dir),
        trace_meta={
            "purpose": "generate_cbmc_contract",
            "summary": f"CBMC contract for {Path(unit_rel).stem}",
            "function_id": function_id_from_extracted_path(unit_rel),
            "function_file": f"extracted_functions/{unit_rel.replace(os.sep, '/')}",
            "language": "c",
        },
    )
    if result is None:
        raise RuntimeError("no valid contract object after the allowed attempts")
    _write_json_atomic(out_path, result)
    return result


# --------------------------------------------------------------------------- #
# Hook
# --------------------------------------------------------------------------- #

def generate_cbmc_contracts(proj_dir: str) -> None:
    """Stage 6 output hook: write one contract object per specced C function."""
    work_dir = Path(proj_dir) / "fm_agent"
    input_dir = work_dir / "extracted_functions"
    out_dir = work_dir / "cbmc"
    trace_dir = work_dir / "trace"
    phases_path = work_dir / "phases.json"
    if not phases_path.is_file():
        raise FileNotFoundError(f"cbmc plugin: phases.json not found at {phases_path}")
    with phases_path.open("r", encoding="utf-8") as file:
        phases_data = json.load(file)

    out_dir.mkdir(parents=True, exist_ok=True)
    system_prompt = _stage_system_prompt(work_dir)

    reused: list[str] = []
    skipped: list[dict] = []
    pending: list[tuple[str, Path]] = []
    for unit_rel in _get_all_phase_files(phases_data, str(input_dir), SOFTWARE_PROFILE):
        unit_rel_posix = unit_rel.replace(os.sep, "/")
        if _path_language(unit_rel) != "c":
            skipped.append({"unit": unit_rel_posix, "reason": "not_c"})
            continue
        if not SOFTWARE_PROFILE.validate(input_dir / unit_rel).ready:
            skipped.append({"unit": unit_rel_posix, "reason": "spec_not_ready"})
            continue
        out_path = out_dir / (unit_rel + CONTRACT_SUFFIX)
        if _load_valid_contract(out_path, unit_rel) is not None:
            reused.append(unit_rel_posix)
            continue
        pending.append((unit_rel, out_path))

    for entry in skipped:
        logging.warning("cbmc plugin: skipping %s (%s)", entry["unit"], entry["reason"])
    print(
        f"[Pipeline] cbmc plugin: {len(pending)} function(s) to contract, "
        f"{len(reused)} reused, {len(skipped)} skipped"
    )

    contracted: list[str] = []
    failed: list[dict] = []
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    _generate_one, system_prompt, input_dir, unit_rel, out_path, trace_dir
                ): unit_rel
                for unit_rel, out_path in pending
            }
            for future in concurrent.futures.as_completed(futures):
                unit_rel = futures[future].replace(os.sep, "/")
                try:
                    future.result()
                except Exception as exc:
                    logging.error("cbmc plugin: %s failed: %s", unit_rel, exc)
                    failed.append({"unit": unit_rel, "error": str(exc)})
                else:
                    contracted.append(unit_rel)
                    logging.info("cbmc plugin: wrote contract for %s", unit_rel)

    summary = {
        "generated_at": utc_now_iso(),
        "model": LLM_MODEL,
        "output_dir": "fm_agent/cbmc",
        "contract_suffix": CONTRACT_SUFFIX,
        "contracted": sorted(contracted),
        "reused": sorted(reused),
        "failed": sorted(failed, key=lambda item: item["unit"]),
        "skipped": sorted(skipped, key=lambda item: item["unit"]),
    }
    _write_json_atomic(out_dir / "summary.json", summary)
    print(
        f"[Pipeline] cbmc plugin: {len(contracted)} contracted, {len(failed)} failed; "
        f"summary at fm_agent/cbmc/summary.json"
    )
    if pending and not contracted:
        raise RuntimeError(
            "cbmc plugin: every contract generation failed; see fm_agent/cbmc/summary.json"
        )
