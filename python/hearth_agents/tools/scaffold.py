"""Test-file scaffold generator for autonomous coding agents.

Writing a test from a blank page is apparently hard enough to fail at 17%
of features (the "zero test files in diff" cluster). This tool generates
a stack-appropriate skeleton from the planner's ``tests`` spec so the
agent only has to fill in assertions, not remember the test harness
boilerplate for each language.

Languages supported (detected from file suffix or explicit ``lang``):
  - ``go``       → *_test.go with TestXxx(t *testing.T) stubs
  - ``ts``/``js``→ *.test.ts with describe/it stubs (Vitest/Jest compatible)
  - ``svelte``   → *.test.ts (same Vitest harness)
  - ``py``       → tests/test_*.py with pytest function stubs
  - ``rs``       → #[cfg(test)] mod block (can't create if file exists)
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool


def _infer_lang(test_file_path: str) -> str:
    p = test_file_path.lower()
    if p.endswith(".go"):
        return "go"
    if p.endswith((".ts", ".tsx")):
        return "ts"
    if p.endswith((".js", ".jsx")):
        return "js"
    if p.endswith(".svelte.test.ts"):
        return "ts"
    if p.endswith(".py"):
        return "py"
    if p.endswith(".rs"):
        return "rs"
    return "unknown"


def _go_skeleton(package: str, cases: list[str]) -> str:
    body = "\n\n".join(
        f"func Test{_camel(c)}(t *testing.T) {{\n\tt.Skip(\"TODO: {c}\")\n}}"
        for c in cases
    )
    return f"package {package}\n\nimport \"testing\"\n\n{body}\n"


def _ts_skeleton(subject: str, cases: list[str]) -> str:
    body = "\n".join(f"  it('{c}', () => {{\n    // TODO: {c}\n  }});" for c in cases)
    return (
        f"import {{ describe, it, expect }} from 'vitest';\n\n"
        f"describe('{subject}', () => {{\n{body}\n}});\n"
    )


def _py_skeleton(cases: list[str]) -> str:
    body = "\n\n".join(
        f"def test_{_snake(c)}():\n    \"\"\"{c}.\"\"\"\n    # TODO: implement"
        for c in cases
    )
    return body + "\n"


def _rs_skeleton(cases: list[str]) -> str:
    body = "\n\n".join(
        f"    #[test]\n    fn test_{_snake(c)}() {{\n        // TODO: {c}\n    }}"
        for c in cases
    )
    return f"#[cfg(test)]\nmod tests {{\n    use super::*;\n\n{body}\n}}\n"


def _camel(s: str) -> str:
    return "".join(w.capitalize() for w in _tokenize(s))


def _snake(s: str) -> str:
    return "_".join(_tokenize(s)).lower()


def _tokenize(s: str) -> list[str]:
    return [w for w in "".join(c if c.isalnum() else " " for c in s).split() if w]


@tool
def scaffold_test_file(test_file_path: str, case_names: list[str], subject: str = "") -> str:
    """Generate a test-file skeleton at ``test_file_path`` with one stub
    per ``case_names`` entry. Language auto-detected from the suffix.

    The file is written directly to disk. If it already exists, returns
    an error instead of overwriting — use ``read_file`` + ``edit_file``
    to add cases to an existing test file.

    After calling this tool, you still need to fill in the assertions
    inside each stub using ``edit_file``. The purpose is to get past
    the blank-page phase (harness imports, describe blocks, package
    declaration) which agents consistently skip — producing the
    "zero test files in diff" block reason at 17% of our failures.

    Args:
        test_file_path: Absolute path for the new test file.
        case_names: One stub per name (free-form English; gets slugified).
        subject: Optional subject for TS describe() block (defaults to
            the file stem).
    """
    path = Path(test_file_path)
    if path.exists():
        return f"error: {test_file_path} already exists; use edit_file to add cases"
    if not case_names:
        return "error: case_names is empty; planner spec must include at least one test case"
    lang = _infer_lang(test_file_path)
    subject = subject or path.stem
    if lang == "go":
        package = path.parent.name or "main"
        content = _go_skeleton(package, case_names)
    elif lang in ("ts", "js"):
        content = _ts_skeleton(subject, case_names)
    elif lang == "py":
        content = _py_skeleton(case_names)
    elif lang == "rs":
        content = _rs_skeleton(case_names)
    else:
        return f"error: unsupported language for {test_file_path} (suffix {path.suffix})"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"error writing {test_file_path}: {e}"
    return (
        f"scaffolded {len(case_names)} test case stub(s) at {test_file_path} "
        f"({lang}); now use edit_file to fill in assertions"
    )
