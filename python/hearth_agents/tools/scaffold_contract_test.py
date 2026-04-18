"""OpenAPI / JSON-Schema contract-test scaffolder.

Research #3835 (autonomous test generation): contract tests derived
from an OpenAPI spec are self-maintaining — when the spec changes,
the tests regenerate. Emits a schemathesis (Python) or @pact-
compatible (TS) skeleton that round-trips requests against the spec.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from langchain_core.tools import tool


def _schemathesis_skeleton(spec_path: str, spec_hash: str) -> str:
    return f"""# Regenerate me when the OpenAPI spec changes.
# Spec:       {spec_path}
# Spec hash:  {spec_hash}
#
# schemathesis property-tests every operation against the declared schema,
# catching: wrong response status codes, missing required fields, type
# mismatches, oversize payloads, invalid content-type. Complements
# behavior tests; does NOT replace them.

import schemathesis

schema = schemathesis.from_path({spec_path!r})


@schema.parametrize()
def test_api_contract(case):
    response = case.call()
    case.validate_response(response)
"""


def _pact_skeleton(spec_path: str, spec_hash: str) -> str:
    return f"""// Regenerate me when the OpenAPI spec changes.
// Spec:       {spec_path}
// Spec hash:  {spec_hash}
//
// Contract test: every request against the declared schema + response
// shape validation. Run via: npm run test:contract.

import {{ OpenAPIBackend }} from 'openapi-backend';
import {{ describe, it, expect }} from 'vitest';

const api = new OpenAPIBackend({{ definition: '{spec_path}' }});

describe('API contract', () => {{
  it.todo('validates all declared endpoints against spec');
}});
"""


@tool
def scaffold_contract_test(
    spec_path: str,
    test_file_path: str,
) -> str:
    """Scaffold a contract test pinned to an OpenAPI spec file.

    The spec's current content hash is embedded in the test file header;
    when the spec changes, re-run this tool to regenerate and the agent
    will see the hash mismatch as a signal to refresh derived tests.

    Args:
        spec_path: path to openapi.yaml / openapi.json.
        test_file_path: path to write the test (``.py`` → schemathesis,
            ``.ts``/``.js`` → openapi-backend).
    """
    spec = Path(spec_path)
    if not spec.exists():
        return f"error: spec {spec_path} not found"
    try:
        spec_hash = hashlib.sha256(spec.read_bytes()).hexdigest()[:12]
    except OSError as e:
        return f"error reading spec: {e}"
    path = Path(test_file_path)
    if path.exists():
        return f"error: {test_file_path} already exists"
    suffix = path.suffix.lower()
    if suffix == ".py":
        content = _schemathesis_skeleton(spec_path, spec_hash)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        content = _pact_skeleton(spec_path, spec_hash)
    else:
        return f"error: unsupported suffix {suffix}; use .py/.ts/.js"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"error writing: {e}"
    return f"scaffolded contract test at {test_file_path} (spec hash {spec_hash})"
