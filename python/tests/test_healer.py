"""Tests for healer._hint_for_reason edge cases."""

from hearth_agents.healer import _hint_for_reason


class TestHintForReason:
    def test_no_commits_hint_is_directive(self):
        hint = _hint_for_reason("no commits on any worktree for feat/foo")
        assert hint.startswith("CRITICAL DIRECTIVE")
        assert "write_file" in hint
        assert "git_commit" in hint
        assert "MUST NOT end a session with zero commits" in hint

    def test_diff_too_large_hint_mentions_line_cap(self):
        hint = _hint_for_reason("diff too large (1000 lines): hearth")
        assert "600-line cap" in hint

    def test_planner_undercount_hint(self):
        hint = _hint_for_reason("planner_undercount: estimated 100, actual 300")
        assert "1.5x" in hint
        assert "planner" in hint.lower()

    def test_no_test_file_hint(self):
        hint = _hint_for_reason("no test file in diff: hearth")
        assert "test" in hint.lower()

    def test_never_pushed_hint(self):
        hint = _hint_for_reason("never pushed: hearth")
        assert "push" in hint.lower()

    def test_tests_failed_hint(self):
        hint = _hint_for_reason("hearth: tests failed")
        assert hint  # not empty
        assert "fail" in hint.lower()

    def test_unknown_reason_returns_empty(self):
        hint = _hint_for_reason("some totally unknown reason")
        assert hint == ""

    def test_empty_reason_returns_empty(self):
        hint = _hint_for_reason("")
        assert hint == ""

    def test_prior_failure_removed_from_no_commits(self):
        """Regression: 'PRIOR FAILURE. Treat this attempt' was removed from hints."""
        hint = _hint_for_reason("no commits on any worktree for feat/foo")
        assert "PRIOR FAILURE. Treat this attempt" not in hint
        assert hint.startswith("CRITICAL DIRECTIVE")
