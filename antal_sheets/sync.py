"""Working out which group memberships should change, and applying them.

Planning is pure (no I/O) so it can be unit tested; ``apply_changes`` does the
Google API calls.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .config import GroupMapping
from .google_groups import GroupsClient, Member

log = logging.getLogger(__name__)

# Only plain members are managed. Owners and managers of a group, and nested
# groups or whole-domain entries, are always left alone.
MANAGED_ROLES = {"MEMBER"}
MANAGED_TYPES = {"USER", "EXTERNAL"}


@dataclass(frozen=True, order=True)
class Change:
    action: str  # "add" or "remove"
    group: str
    email: str

    def __str__(self) -> str:
        sign = "+" if self.action == "add" else "-"
        return f"{sign} {self.email} {'to' if self.action == 'add' else 'from'} {self.group}"


def groups_for_roles(groups: Iterable[GroupMapping], roles: set[int]) -> list[GroupMapping]:
    return [g for g in groups if g.roles & roles]


def plan_full(
    groups: Iterable[GroupMapping],
    member_roles: Mapping[int, set[int]],
    links: Mapping[int, str],
    actual: Mapping[str, list[Member]],
) -> list[Change]:
    """Diff every group against who should be in it.

    ``member_roles`` must contain every current guild member (Discord id to
    role ids); anyone missing is treated as having left the server.
    """
    changes: list[Change] = []
    for g in groups:
        desired = {
            links[uid]
            for uid, roles in member_roles.items()
            if uid in links and g.roles & roles
        }
        present = {m.email for m in actual.get(g.email, [])}
        managed = {
            m.email
            for m in actual.get(g.email, [])
            if m.role in MANAGED_ROLES and m.type in MANAGED_TYPES
        }
        for email in sorted(desired - present):
            changes.append(Change("add", g.email, email))
        for email in sorted(managed - desired - g.protected):
            changes.append(Change("remove", g.email, email))
    return changes


def plan_user(
    groups: Iterable[GroupMapping],
    roles: set[int] | None,
    email: str | None,
    stale_emails: Iterable[str] = (),
) -> list[Change]:
    """Changes for a single Discord user.

    ``roles`` is None when the user is no longer in the server. ``stale_emails``
    are Google accounts the user was previously linked with and should lose
    access entirely. Adds and removes are idempotent on the Google side, so
    this does not need to know current group state.
    """
    changes: list[Change] = []
    stale = {e.lower() for e in stale_emails if e}
    if email:
        stale.discard(email)
    for g in groups:
        if email:
            if roles and g.roles & roles:
                changes.append(Change("add", g.email, email))
            elif email not in g.protected:
                changes.append(Change("remove", g.email, email))
        for old in sorted(stale - g.protected):
            changes.append(Change("remove", g.email, old))
    return changes


@dataclass
class ApplyResult:
    applied: list[Change]
    failed: list[tuple[Change, str]]
    skipped_removals: list[Change]


def apply_changes(
    client: GroupsClient,
    changes: list[Change],
    *,
    dry_run: bool,
    max_removals: int | None = None,
) -> ApplyResult:
    """Apply changes to Google. Blocking; run it in a worker thread.

    If ``max_removals`` is set and the plan removes more people than that,
    removals are held back (adds still go through) so a Discord outage or a
    config mistake can't empty the groups.
    """
    result = ApplyResult([], [], [])
    removals = [c for c in changes if c.action == "remove"]
    hold_removals = max_removals is not None and len(removals) > max_removals
    if hold_removals:
        log.warning(
            "Plan removes %d members, over the limit of %d; holding removals back",
            len(removals),
            max_removals,
        )

    for change in changes:
        if change.action == "remove" and hold_removals:
            result.skipped_removals.append(change)
            continue
        if dry_run:
            log.info("[dry run] %s", change)
            result.applied.append(change)
            continue
        try:
            if change.action == "add":
                client.add_member(change.group, change.email)
            else:
                client.remove_member(change.group, change.email)
            log.info("%s", change)
            result.applied.append(change)
        except Exception as exc:  # keep going; one bad address shouldn't stop the run
            log.exception("Failed: %s", change)
            result.failed.append((change, str(exc)))
    return result
