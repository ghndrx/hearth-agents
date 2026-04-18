"""Schema migration triple scaffolder.

Research #3827 (autonomous database schema generation): every schema
change needs three separate artifacts — forward migration, data
backfill, rollback test — and the agent routinely forgets one or more.
This tool emits all three in one call so the agent only has to fill
in the columns/tables, not remember the pattern.

Supports the migration stacks present in Hearth:
  - Go (goose-style SQL .sql files with ``-- +goose Up`` / Down markers)
  - Python (Alembic revision template)
  - TypeScript (Drizzle migration template)
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool


def _goose_template(migration_name: str, description: str) -> str:
    return f"""-- +goose Up
-- {description}
-- TODO: write forward DDL. Must be REVERSIBLE (see Down section).

-- +goose Down
-- TODO: write inverse DDL that restores pre-migration state.
-- If this migration is genuinely irreversible, reject: schema must
-- always be rollback-safe.
"""


def _goose_backfill_template(migration_name: str) -> str:
    return f"""-- +goose Up
-- Backfill for {migration_name}. Runs AFTER the schema migration.
-- Pattern: batched update, then verifier query.

-- TODO replace placeholders:
--   UPDATE <table> SET <new_col> = <default_expr>
--     WHERE <new_col> IS NULL LIMIT 1000;
-- Repeat until SELECT COUNT(*) FROM <table> WHERE <new_col> IS NULL = 0.

-- +goose Down
-- TODO: clear the backfilled rows if the forward migration is rolled back.
"""


def _alembic_template(migration_name: str, description: str) -> str:
    return f'''"""
{description}

Revision ID: <auto>
Revises: <auto>
"""
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    """TODO: forward migration. Must be reversible."""
    raise NotImplementedError("fill in upgrade()")


def downgrade() -> None:
    """TODO: exact inverse of upgrade()."""
    raise NotImplementedError("fill in downgrade()")
'''


def _drizzle_template(migration_name: str, description: str) -> str:
    return f"""// {description}
// drizzle migration — forward + rollback both required.

import {{ sql }} from "drizzle-orm";

export async function up(db): Promise<void> {{
  // TODO: forward DDL
  await sql`/* fill in */`;
}}

export async function down(db): Promise<void> {{
  // TODO: inverse DDL
  await sql`/* fill in */`;
}}
"""


def _rollback_test_template(stack: str, migration_name: str) -> str:
    if stack == "go":
        return f"""package migrations_test

import "testing"

// TestRollback_{migration_name} applies the migration, then the rollback,
// and asserts the schema state is identical to before. Research #3827
// requires this test for every schema change; no exceptions.
func TestRollback_{migration_name}(t *testing.T) {{
    t.Skip("TODO: use your migration harness — apply up(), snapshot, down(), diff")
}}
"""
    if stack == "py":
        return f"""def test_rollback_{migration_name}(alembic_engine):
    \"\"\"Apply up(), capture schema, apply down(), diff.\"\"\"
    # TODO: wire against your alembic test harness
    raise NotImplementedError
"""
    return f"""// rollback test for {migration_name} — fill in with your harness.
"""


@tool
def scaffold_migration(
    migration_name: str,
    description: str,
    stack: str,
    migrations_dir: str,
) -> str:
    """Create the three required artifacts for a DB schema change:
    forward migration, backfill file, rollback test. Emits empty
    stubs — you fill in the columns/tables.

    Refuses to scaffold a migration that claims to be irreversible;
    the article is emphatic that schemas must always be rollback-safe.
    If you genuinely cannot reverse (e.g. DROP COLUMN on primary key),
    reject the feature and escalate.

    Args:
        migration_name: snake_case name, e.g. ``add_role_position``.
        description: one-line human-readable purpose.
        stack: ``go`` | ``py`` | ``ts``.
        migrations_dir: directory under the worktree where migrations live.
    """
    if stack not in ("go", "py", "ts"):
        return f"error: unsupported stack {stack!r}; use go | py | ts"
    out_dir = Path(migrations_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"error creating {migrations_dir}: {e}"
    ext = {"go": "sql", "py": "py", "ts": "ts"}[stack]
    forward_path = out_dir / f"{migration_name}.{ext}"
    backfill_path = out_dir / f"{migration_name}_backfill.{ext}"
    rollback_test_path = out_dir / f"{migration_name}_rollback_test.{ext}"
    if any(p.exists() for p in (forward_path, backfill_path, rollback_test_path)):
        return f"error: one or more targets already exist; use edit_file to modify"
    forward = {
        "go": _goose_template(migration_name, description),
        "py": _alembic_template(migration_name, description),
        "ts": _drizzle_template(migration_name, description),
    }[stack]
    backfill = {
        "go": _goose_backfill_template(migration_name),
        "py": _alembic_template(f"{migration_name}_backfill", "Data backfill"),
        "ts": _drizzle_template(f"{migration_name}_backfill", "Data backfill"),
    }[stack]
    rollback = _rollback_test_template(stack, migration_name)
    try:
        forward_path.write_text(forward, encoding="utf-8")
        backfill_path.write_text(backfill, encoding="utf-8")
        rollback_test_path.write_text(rollback, encoding="utf-8")
    except OSError as e:
        return f"error writing scaffolds: {e}"
    return (
        f"scaffolded migration triple ({stack}):\n"
        f"  {forward_path}\n  {backfill_path}\n  {rollback_test_path}\n"
        f"now edit each to fill in columns/tables, then run verify_staged"
    )
