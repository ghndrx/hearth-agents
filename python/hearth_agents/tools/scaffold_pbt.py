"""Property-based test scaffolder (Hypothesis / fast-check style).

Research #3835 (autonomous test generation): PBT derives tests from
signatures and invariants rather than hand-written cases. The
shrinker produces minimal failing inputs when something breaks.
Works well alongside our existing ``scaffold_test_file`` (example-based
unit tests) — PBT for invariants, unit for specific golden cases.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool


def _hypothesis_skeleton(module: str, function: str, params: list[str], invariants: list[str]) -> str:
    # Default to st.text() | st.integers() — agent refines via edit_file.
    strategies = "\n".join(f"    {p}=st.text() | st.integers(),  # TODO: tighten strategy" for p in params)
    inv_lines = "\n".join(f"    # INVARIANT: {inv}" for inv in invariants) or "    # INVARIANT: <fill in what must always hold>"
    return f"""from hypothesis import given, strategies as st, example

from {module} import {function}


@given(
{strategies}
)
@example()  # add known-good edge cases here as keyword-args
def test_{function}_property(**kwargs):
    result = {function}(**kwargs)
{inv_lines}
    assert result is not None  # replace with real invariant check
"""


def _fastcheck_skeleton(function: str, params: list[str], invariants: list[str]) -> str:
    arbs = ", ".join(f"fc.anything()" for _ in params) or "fc.anything()"
    inv_lines = "\n  ".join(f"// INVARIANT: {inv}" for inv in invariants) or "// INVARIANT: <fill in>"
    return f"""import fc from 'fast-check';
import {{ describe, it, expect }} from 'vitest';
import {{ {function} }} from './{function}';

describe('{function} — property-based', () => {{
  it('maintains invariants for all valid inputs', () => {{
    fc.assert(
      fc.property({arbs}, (...args) => {{
        const result = {function}(...args);
        {inv_lines}
        return result !== undefined;  // replace with real invariant
      }}),
    );
  }});
}});
"""


@tool
def scaffold_pbt(
    test_file_path: str,
    function: str,
    params: list[str],
    invariants: list[str],
    module: str = "",
) -> str:
    """Scaffold a property-based test. Language inferred from suffix:
    ``.py`` → Hypothesis, ``.ts``/``.js`` → fast-check.

    Args:
        test_file_path: absolute path to write the test file.
        function: name of the function under test.
        params: parameter names to generate strategies for.
        invariants: human-readable invariants the function should uphold
            — one per line in the output comments; agent fills in the
            actual assert statements via ``edit_file``.
        module: Python import path (e.g. ``my.package``); required for .py.
    """
    path = Path(test_file_path)
    if path.exists():
        return f"error: {test_file_path} already exists"
    suffix = path.suffix.lower()
    if suffix == ".py":
        if not module:
            return "error: Python PBT scaffold needs module= import path"
        content = _hypothesis_skeleton(module, function, params, invariants)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        content = _fastcheck_skeleton(function, params, invariants)
    else:
        return f"error: unsupported suffix {suffix}; use .py/.ts/.js"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"error writing {test_file_path}: {e}"
    return f"scaffolded PBT at {test_file_path}; fill in invariants + tighten strategies via edit_file"
