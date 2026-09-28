"""Thin client for the Admin SDK Directory API group-membership endpoints.

The calls are blocking (google-auth uses requests), so the bot runs them
through ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/admin.directory.group.member"]
API = "https://admin.googleapis.com/admin/directory/v1"


@dataclass(frozen=True)
class Member:
    email: str
    role: str  # OWNER, MANAGER or MEMBER
    type: str  # USER, GROUP, CUSTOMER, EXTERNAL


class GroupsClient:
    def __init__(self, service_account_file: str, delegated_admin: str | None = None):
        creds = service_account.Credentials.from_service_account_file(
            service_account_file, scopes=SCOPES
        )
        if delegated_admin:
            creds = creds.with_subject(delegated_admin)
        self._session = AuthorizedSession(creds)

    def list_members(self, group: str) -> list[Member]:
        members: list[Member] = []
        params: dict[str, str] = {"maxResults": "200"}
        while True:
            resp = self._session.get(f"{API}/groups/{quote(group)}/members", params=params)
            resp.raise_for_status()
            body = resp.json()
            for m in body.get("members", []):
                if "email" in m:
                    members.append(Member(m["email"].lower(), m.get("role", "MEMBER"), m.get("type", "USER")))
            token = body.get("nextPageToken")
            if not token:
                return members
            params["pageToken"] = token

    def add_member(self, group: str, email: str) -> None:
        resp = self._session.post(
            f"{API}/groups/{quote(group)}/members", json={"email": email, "role": "MEMBER"}
        )
        if resp.status_code == 409:  # already a member
            return
        resp.raise_for_status()

    def remove_member(self, group: str, email: str) -> None:
        resp = self._session.delete(f"{API}/groups/{quote(group)}/members/{quote(email)}")
        if resp.status_code == 404:  # already gone
            return
        resp.raise_for_status()
