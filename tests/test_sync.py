from antal_sheets.config import GroupMapping
from antal_sheets.db import Database, EmailAlreadyLinked
from antal_sheets.google_groups import Member
from antal_sheets.sync import Change, apply_changes, plan_full, plan_user

import pytest

PLEDGED = 1
LOGISTICS = 2
OFFICER = 3

GROUPS = (
    GroupMapping("pledged@x.org", frozenset({PLEDGED, OFFICER}), frozenset({"owner@gmail.com"})),
    GroupMapping("logi@x.org", frozenset({LOGISTICS})),
)


def m(email, role="MEMBER", type_="USER"):
    return Member(email, role, type_)


def test_full_adds_missing_and_removes_departed():
    member_roles = {10: {PLEDGED}, 11: {PLEDGED, LOGISTICS}, 12: set()}
    links = {10: "a@gmail.com", 11: "b@gmail.com", 12: "c@gmail.com", 99: "gone@gmail.com"}
    actual = {
        "pledged@x.org": [m("a@gmail.com"), m("c@gmail.com"), m("gone@gmail.com")],
        "logi@x.org": [],
    }
    changes = plan_full(GROUPS, member_roles, links, actual)
    assert set(changes) == {
        Change("add", "pledged@x.org", "b@gmail.com"),
        Change("remove", "pledged@x.org", "c@gmail.com"),  # lost the role
        Change("remove", "pledged@x.org", "gone@gmail.com"),  # left the server
        Change("add", "logi@x.org", "b@gmail.com"),
    }


def test_full_never_touches_owners_managers_protected_or_nested_groups():
    actual = {
        "pledged@x.org": [
            m("boss@x.org", role="OWNER"),
            m("mod@gmail.com", role="MANAGER"),
            m("owner@gmail.com"),  # protected
            m("sub@x.org", type_="GROUP"),
            m("stranger@gmail.com"),
        ],
        "logi@x.org": [],
    }
    changes = plan_full(GROUPS, {10: {PLEDGED}}, {}, actual)
    assert changes == [Change("remove", "pledged@x.org", "stranger@gmail.com")]


def test_full_unlinked_members_are_ignored():
    changes = plan_full(GROUPS, {10: {PLEDGED}}, {}, {"pledged@x.org": [], "logi@x.org": []})
    assert changes == []


def test_user_role_gain_and_loss():
    changes = plan_user(GROUPS, {PLEDGED}, "a@gmail.com")
    assert changes == [
        Change("add", "pledged@x.org", "a@gmail.com"),
        Change("remove", "logi@x.org", "a@gmail.com"),
    ]


def test_user_left_server_removes_everywhere():
    changes = plan_user(GROUPS, None, "a@gmail.com")
    assert {c.action for c in changes} == {"remove"}
    assert len(changes) == 2


def test_user_relinked_removes_old_email():
    changes = plan_user(GROUPS, {LOGISTICS}, "new@gmail.com", ["old@gmail.com"])
    assert Change("remove", "pledged@x.org", "old@gmail.com") in changes
    assert Change("remove", "logi@x.org", "old@gmail.com") in changes
    assert Change("add", "logi@x.org", "new@gmail.com") in changes


class FakeClient:
    def __init__(self):
        self.calls = []

    def add_member(self, group, email):
        self.calls.append(("add", group, email))

    def remove_member(self, group, email):
        if email == "boom@gmail.com":
            raise RuntimeError("api error")
        self.calls.append(("remove", group, email))


def test_apply_dry_run_makes_no_calls():
    client = FakeClient()
    result = apply_changes(client, [Change("add", "g", "a@gmail.com")], dry_run=True)
    assert client.calls == [] and len(result.applied) == 1


def test_apply_holds_back_mass_removals_but_still_adds():
    client = FakeClient()
    changes = [Change("add", "g", "new@gmail.com")] + [
        Change("remove", "g", f"u{i}@gmail.com") for i in range(5)
    ]
    result = apply_changes(client, changes, dry_run=False, max_removals=3)
    assert client.calls == [("add", "g", "new@gmail.com")]
    assert len(result.skipped_removals) == 5


def test_apply_continues_after_failure():
    client = FakeClient()
    changes = [Change("remove", "g", "boom@gmail.com"), Change("remove", "g", "ok@gmail.com")]
    result = apply_changes(client, changes, dry_run=False)
    assert len(result.failed) == 1 and client.calls == [("remove", "g", "ok@gmail.com")]


def test_db_links_and_states(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    assert db.set_link(1, "A@Gmail.com") is None
    assert db.get_email(1) == "a@gmail.com"
    with pytest.raises(EmailAlreadyLinked):
        db.set_link(2, "a@gmail.com")
    assert db.set_link(1, "b@gmail.com") == "a@gmail.com"

    state = db.create_state(5)
    assert db.peek_state(state) == 5
    assert db.consume_state(state) == 5
    assert db.consume_state(state) is None


def test_sheet_shared_with_two_groups_listed_once():
    from antal_sheets.bot import _sheets
    from antal_sheets.config import Sheet

    shared = Sheet("Targets", "https://docs.google.com/spreadsheets/d/T")
    g1 = GroupMapping("pledged@x.org", frozenset({PLEDGED}), sheets=(shared,))
    g2 = GroupMapping("trusted@x.org", frozenset({OFFICER}), sheets=(shared, Sheet("Ops", "u2")))
    assert [s.name for s in _sheets([g1, g2])] == ["Targets", "Ops"]
