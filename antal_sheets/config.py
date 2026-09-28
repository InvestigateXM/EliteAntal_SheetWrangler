"""Configuration loading.

Non-secret settings live in a YAML file; secrets come from environment
variables so the config file can be committed or shared safely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Sheet:
    name: str
    url: str


@dataclass(frozen=True)
class GroupMapping:
    """One Google group, and the Discord roles that grant membership in it."""

    email: str
    roles: frozenset[int]
    # Addresses the bot never removes from this group (co-owners, officers
    # added by hand, etc.). Compared case-insensitively.
    protected: frozenset[str] = frozenset()
    # Purely informational: shown to players in /status and link DMs.
    sheets: tuple[Sheet, ...] = ()


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int
    admin_role_ids: frozenset[int]
    log_channel_id: int | None

    groups: tuple[GroupMapping, ...]

    service_account_file: str
    # Optional: admin user to impersonate via domain-wide delegation. Leave
    # empty when the service account has the Groups Admin role itself.
    delegated_admin: str | None

    oauth_client_id: str
    oauth_client_secret: str
    public_base_url: str
    listen_host: str
    listen_port: int

    database_path: str
    reconcile_interval_minutes: int
    dry_run: bool
    max_removals_per_run: int

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/oauth/callback"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable {name}")
    return value


def load_config(path: str | os.PathLike) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}

    discord = raw.get("discord", {})
    google = raw.get("google", {})
    web = raw.get("web", {})
    sync = raw.get("sync", {})

    groups = []
    for g in raw.get("groups", []):
        groups.append(
            GroupMapping(
                email=g["email"].strip().lower(),
                roles=frozenset(int(r) for r in g.get("roles", [])),
                protected=frozenset(p.strip().lower() for p in g.get("protected", [])),
                sheets=tuple(Sheet(s["name"], s["url"]) for s in g.get("sheets", [])),
            )
        )
    if not groups:
        raise SystemExit("Config has no groups; nothing to manage")

    return Config(
        discord_token=_require_env("DISCORD_TOKEN"),
        guild_id=int(discord["guild_id"]),
        admin_role_ids=frozenset(int(r) for r in discord.get("admin_roles", [])),
        log_channel_id=int(discord["log_channel_id"]) if discord.get("log_channel_id") else None,
        groups=tuple(groups),
        service_account_file=os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        or google.get("service_account_file")
        or "service-account.json",
        delegated_admin=google.get("delegated_admin") or None,
        oauth_client_id=google["oauth_client_id"],
        oauth_client_secret=_require_env("GOOGLE_OAUTH_CLIENT_SECRET"),
        public_base_url=web["public_base_url"],
        listen_host=web.get("listen_host", "0.0.0.0"),
        listen_port=int(web.get("listen_port", 8080)),
        database_path=raw.get("database_path", "data/links.sqlite3"),
        reconcile_interval_minutes=int(sync.get("reconcile_interval_minutes", 60)),
        dry_run=bool(sync.get("dry_run", True)),
        max_removals_per_run=int(sync.get("max_removals_per_run", 25)),
    )
