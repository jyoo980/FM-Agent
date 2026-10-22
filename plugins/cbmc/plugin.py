"""Translate FM-Agent's natural-language function specs into CBMC contracts.

Stage 6 output hook: runs after every ``.spec.json`` exists and writes one
``<Func>.c.cbmc.json`` per C function under ``fm_agent/cbmc/``.
"""

from plugins.cbmc.contracts import generate_cbmc_contracts as _generate


def generate_cbmc_contracts(proj_dir: str) -> None:
    """Emit a CBMC contract object for every specced C function."""
    _generate(proj_dir)
