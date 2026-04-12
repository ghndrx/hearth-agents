"""Backlog persistence and priority ordering."""

from hearth_agents.backlog import Backlog, Feature


def test_next_pending_prefers_higher_priority(tmp_path):
    b = Backlog(str(tmp_path / "backlog.json"))
    b.features = [
        Feature(id="a", name="a", description="", priority="low"),
        Feature(id="b", name="b", description="", priority="critical"),
        Feature(id="c", name="c", description="", priority="high"),
    ]
    assert b.next_pending().id == "b"


def test_set_status_persists(tmp_path):
    path = str(tmp_path / "backlog.json")
    b = Backlog(path)
    first_id = b.features[0].id
    b.set_status(first_id, "done")

    reopened = Backlog(path)
    assert next(f for f in reopened.features if f.id == first_id).status == "done"


def test_add_rejects_duplicate(tmp_path):
    b = Backlog(str(tmp_path / "backlog.json"))
    f = Feature(id="x", name="x", description="")
    assert b.add(f) is True
    assert b.add(f) is False
