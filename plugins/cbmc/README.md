# cbmc plugin

Turns FM-Agent's natural-language function specifications into CBMC function
contract objects. For every extracted C function with a ready `.spec.json`, one
structured LLM call produces the `__CPROVER_requires` / `__CPROVER_ensures` /
`__CPROVER_assigns` clause expressions for that function.

The plugin then inserts those clauses into annotated copies of the C sources. It
does not modify the project's sources, generate harnesses, or run `cbmc`.

## Usage

```bash
uv run python main.py <proj_dir> --plugin cbmc --only-spec
```

`--only-spec` skips the LLM reasoner and bug validation; drop it if you also want
those. The plugin runs as the Stage 6 output hook, after every specification exists.
It uses the configured `[llm]` model and `[runtime] max_workers`, and the structured
JSON retry limit `max_spec_iter`. `--resume` and `--isolate` are supported;
`--incremental` is not (incremental runs do not execute plugin hooks).

## Output

```
fm_agent/cbmc/
├── summary.json                                  # contracted / reused / failed / skipped / annotated
├── <src_dir>/<file>-c/<Func>.c.cbmc.json         # one object per function
└── annotated/<src_dir>/<file>.c                  # copy of each C source with contracts inserted
fm_agent/cbmc_contract_system_prompt.md           # the prompt this run used
```

The output tree mirrors `fm_agent/extracted_functions/`, so
`src/parser.c` → `parse_header` yields
`fm_agent/cbmc/src/parser-c/parse_header.c.cbmc.json`:

```json
{
  "function": "parse_header",
  "signature": "int parse_header(const char *buf, size_t len, hdr_t *out)",
  "requires": ["len > 0", "__CPROVER_is_fresh(buf, len)", "__CPROVER_is_fresh(out, sizeof(*out))"],
  "ensures": ["__CPROVER_return_value == 0 || __CPROVER_return_value == -1",
              "__CPROVER_return_value == -1 ==> *out == __CPROVER_old(*out)"],
  "assigns": ["*out"],
  "unexpressible": [
    {"spec_fragment": "buf holds a NUL-terminated header",
     "reason": "no bounded way to state NUL-termination without a constant length"}
  ]
}
```

Each list entry is the bare expression for one clause, so a contract on the
function definition is `__CPROVER_requires(<entry>)` per `requires` entry, and so on.

Every object is validated before it is written: exact keys, non-empty strings,
balanced brackets, no trailing `;`, `__CPROVER_return_value` / `__CPROVER_old` only in
`ensures`, and at least one clause or one `unexpressible` entry. Invalid responses
are sent back to the model with the error, up to `max_spec_iter` times. Functions
that still fail are listed under `failed` in `summary.json` and the run continues;
the hook raises only if nothing could be contracted.

## Annotated sources

Every C source file in `phases.json` is copied to `fm_agent/cbmc/annotated/` at the
same relative path. Each function that has a valid contract gets its clauses
between the declarator and the body, with its `unexpressible` entries as a comment
block above it:

```c
/* CBMC contract: not expressible
 * - buf holds a NUL-terminated header: no bounded way to state NUL-termination ...
 */
int parse_header(const char *buf, size_t len, hdr_t *out)
    __CPROVER_requires(len > 0)
    __CPROVER_ensures(__CPROVER_return_value == 0 || __CPROVER_return_value == -1)
    __CPROVER_assigns(*out)
{
```

Each `assigns` entry becomes its own clause, because a `cond: target` entry would
otherwise guard the targets after it. An empty `assigns` list becomes
`__CPROVER_assigns()`. Quantifier variables are renamed (`i` -> `i_1`) when two
clauses of one function bind the same name, since CBMC scopes them together.
Functions without a valid contract are copied unchanged and listed under
`annotated.missing` in `summary.json`.

The copies live outside the project tree, so point the include path back at it:

```bash
goto-cc -I <proj_dir> -c <proj_dir>/fm_agent/cbmc/annotated/src/parser.c -o parser.goto
goto-instrument --enforce-contract parse_header parser.goto checked.goto
cbmc checked.goto --function parse_header
```

The JSON validation is syntactic, so `goto-cc` can still reject a clause. For
example, `errno` is a macro that expands to a call on some platforms, and
`__CPROVER_old` of a compound expression such as `a + b` is unsupported (use
`__CPROVER_old(a) + __CPROVER_old(b)`). Fix the `.cbmc.json` file and rerun with
`--resume` to regenerate the copy.

## Resume

A sidecar that already validates is reused without an LLM call. Delete or corrupt a
`.cbmc.json` and rerun with `--resume` to regenerate only that function.

## Limitations

- C only. Functions in `.h` files (which FM-Agent treats as C++) and all other
  languages are listed under `skipped` with reason `not_c`.
- Only one plugin is active per run, so `cbmc` cannot combine with `--entry-func`
  or `--plugin chip`.
- The model sees the extracted function, its spec, and its callee expectations, not
  the surrounding headers. Struct fields and macros it names come from the function
  body; anything the spec mentions that is not visible there tends to land in
  `unexpressible`.
- Quantifier bounds that are runtime values are accepted by CBMC's SMT backend but
  not its default SAT backend; the prompt asks the model to flag such cases.
