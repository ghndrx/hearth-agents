"""Accessibility audit wrapper (research #3838).

Runs axe-core's CLI (``@axe-core/cli``) against a URL or HTML file
and parses the violation list into a structured report the agent can
use to drive remediation. ~57% of WCAG issues are auto-detectable;
the remaining 43% need keyboard / screen-reader review by a human,
which we don't attempt.

Best-effort: if axe-core isn't installed, returns a clear install
hint rather than silently failing. Parser is defensive against
axe-cli output format changes (major/minor rewrites every few years).
"""

from __future__ import annotations

import json
import subprocess

from langchain_core.tools import tool


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"axe-core timed out after {timeout}s"
    except FileNotFoundError:
        return 127, (
            "axe-core CLI not found. Install with: "
            "npm install -g @axe-core/cli. Research #3838 cites ~57% WCAG "
            "auto-detection — still valuable even without the other 43%."
        )


@tool
def a11y_audit(url_or_file: str, save_to: str = "") -> str:
    """Run axe-core against ``url_or_file`` (a live URL or a local
    built-HTML path) and return a prioritized violation summary.

    Reports violations grouped by impact (critical/serious/moderate/
    minor), each with rule id + selector sample + remediation hint.
    When ``save_to`` is provided, the raw axe-core JSON is written
    there too for post-processing.

    Use on the output of ``npm run build`` before opening an auto-PR;
    a11y regressions are cheaper to fix in the same commit than after
    shipping.
    """
    cmd = ["axe", url_or_file, "--exit"]
    if save_to:
        cmd.extend(["--save", save_to])
    cmd.extend(["--stdout"])
    code, out = _run(cmd, timeout=180)
    if code == 127:
        return out  # install hint
    # axe-cli exits 1 when violations found; that's fine — we parse either way.
    # Best effort: try JSON, then fall back to line scraping.
    violations: list[dict] = []
    try:
        data = json.loads(out)
        if isinstance(data, list):
            data = data[0] if data else {}
        violations = data.get("violations") or []
    except json.JSONDecodeError:
        pass
    if not violations:
        if "0 violations" in out.lower() or code == 0:
            return "✓ no accessibility violations detected (automated scan only; manual review still needed)"
        return f"axe-core ran but produced no parseable violations:\n{out[-600:]}"
    by_impact: dict[str, list[dict]] = {}
    for v in violations:
        impact = v.get("impact") or "minor"
        by_impact.setdefault(impact, []).append(v)
    order = ["critical", "serious", "moderate", "minor"]
    lines = [f"axe-core found {len(violations)} violation(s):"]
    for impact in order:
        vs = by_impact.get(impact, [])
        if not vs:
            continue
        lines.append(f"\n## {impact} ({len(vs)})")
        for v in vs[:6]:
            rule = v.get("id", "?")
            nodes = v.get("nodes") or []
            selector = nodes[0].get("target", [""])[0] if nodes else ""
            help_text = v.get("help", "")
            lines.append(f"  - {rule}: {help_text[:80]}")
            if selector:
                lines.append(f"      selector: {selector[:100]}")
    return "\n".join(lines)
