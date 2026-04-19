"""Fast coverage for small pure-function modules: commitlint,
auto_label, classify_bump. No network, no disk."""

from __future__ import annotations

import pytest

pytest.importorskip("structlog")  # upstream imports logger → structlog


def test_commitlint_parse_header() -> None:
    from hearth_agents.commitlint import parse
    p = parse("feat(voice): add always-on channels\n\nbody...")
    assert p is not None
    assert p.type == "feat"
    assert p.scope == "voice"
    assert not p.breaking
    assert p.bump == "minor"


def test_commitlint_parse_breaking() -> None:
    from hearth_agents.commitlint import parse
    # bang marker
    p = parse("feat!: rename auth API")
    assert p is not None and p.breaking and p.bump == "major"
    # footer marker
    p = parse("fix: remove deprecated endpoint\n\nBREAKING CHANGE: removed /v1/auth")
    assert p is not None and p.breaking and p.bump == "major"


def test_commitlint_next_bump() -> None:
    from hearth_agents.commitlint import parse, next_bump
    commits = [parse("fix: small"), parse("feat: medium"), parse("chore: noise")]
    assert next_bump([c for c in commits if c]) == "minor"


def test_commitlint_render_changelog() -> None:
    from hearth_agents.commitlint import parse, render_changelog
    commits = [
        parse("feat: one"),
        parse("fix(auth): two"),
        parse("chore: ignored"),
    ]
    md = render_changelog([c for c in commits if c])
    assert "### Features" in md and "one" in md
    assert "### Bug Fixes" in md and "two" in md
    assert "ignored" not in md  # chore excluded


def test_auto_label_rules() -> None:
    from hearth_agents.auto_label import infer_labels
    assert "auth" in infer_labels("add login form", "with JWT token")
    assert "messaging" in infer_labels("thread reactions", "users can reply to a message")
    assert infer_labels("random", "nothing here") == []
    # Cap enforced
    labels = infer_labels("migration for auth api voice channel", "schema + oauth + livekit")
    assert len(labels) <= 3


def test_classify_bump_semver() -> None:
    from hearth_agents.tools.classify_bump import classify
    assert classify("1.2.3", "1.2.4") == "patch"
    assert classify("1.2.3", "1.3.0") == "minor"
    assert classify("1.2.3", "2.0.0") == "major"
    assert classify("v1.0.0", "v1.0.0") == "patch"  # identical
    assert classify("not-semver", "1.2.3") == "unknown"
    # Non-strict
    assert classify("2.0-beta9", "2.0-beta10") in ("patch", "unknown")
