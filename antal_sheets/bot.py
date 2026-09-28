"""The Discord bot: slash commands, member events and the periodic reconcile."""

from __future__ import annotations

import asyncio
import logging

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks

from .config import Config, GroupMapping, Sheet
from .db import Database, EmailAlreadyLinked
from .google_groups import GroupsClient
from .sync import ApplyResult, Change, apply_changes, groups_for_roles, plan_full, plan_user
from .web import build_app

log = logging.getLogger(__name__)


def _sheets(groups: list[GroupMapping]) -> list[Sheet]:
    """Sheets reachable through these groups, once each even if shared with several."""
    seen: dict[str, Sheet] = {}
    for g in groups:
        for s in g.sheets:
            seen.setdefault(s.url, s)
    return list(seen.values())


def _sheet_lines(sheets: list[Sheet]) -> str:
    lines = [f"• [{s.name}]({s.url})" for s in sheets]
    return "\n".join(lines) if lines else "• (no sheets listed)"


def _summary(result: ApplyResult, dry_run: bool, limit: int = 30) -> str:
    head = "Planned changes (dry run, nothing applied)" if dry_run else "Applied changes"
    lines = [f"**{head}:** {len(result.applied)}"]
    lines += [f"`{c}`" for c in result.applied[:limit]]
    if len(result.applied) > limit:
        lines.append(f"…and {len(result.applied) - limit} more")
    if result.skipped_removals:
        lines.append(
            f"**Held back {len(result.skipped_removals)} removals** (over the per-run limit). "
            "Check the Discord side, then raise `max_removals_per_run` or run `/sync` again."
        )
    if result.failed:
        lines.append(f"**Failed:** {len(result.failed)}")
        lines += [f"`{c}`: {err[:120]}" for c, err in result.failed[:10]]
    return "\n".join(lines)


class SheetBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True  # privileged: enable "Server Members Intent" in the developer portal
        super().__init__(intents=intents)
        self.config = config
        self.db = Database(config.database_path)
        self.groups = GroupsClient(config.service_account_file, config.delegated_admin)
        self.tree = app_commands.CommandTree(self)
        self._sync_lock = asyncio.Lock()
        self._web_runner: web.AppRunner | None = None
        self._register_commands()

    # --- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        self._web_runner = web.AppRunner(build_app(self))
        await self._web_runner.setup()
        await web.TCPSite(self._web_runner, self.config.listen_host, self.config.listen_port).start()
        log.info("Link server listening on %s:%s", self.config.listen_host, self.config.listen_port)

        self.reconcile_loop.change_interval(minutes=self.config.reconcile_interval_minutes)
        self.reconcile_loop.start()

    async def close(self) -> None:
        self.reconcile_loop.cancel()
        if self._web_runner:
            await self._web_runner.cleanup()
        self.db.close()
        await super().close()

    @property
    def guild(self) -> discord.Guild | None:
        return self.get_guild(self.config.guild_id)

    # --- syncing ---------------------------------------------------------

    async def _apply(self, changes: list[Change], *, max_removals: int | None = None) -> ApplyResult:
        return await asyncio.to_thread(
            apply_changes,
            self.groups,
            changes,
            dry_run=self.config.dry_run,
            max_removals=max_removals,
        )

    async def sync_member(self, discord_id: int, stale_emails: tuple[str, ...] = ()) -> ApplyResult:
        """Bring one user's group memberships in line with their current roles."""
        guild = self.guild
        member = guild.get_member(discord_id) if guild else None
        roles = {r.id for r in member.roles} if member else None
        email = self.db.get_email(discord_id)
        changes = plan_user(self.config.groups, roles, email, stale_emails)
        async with self._sync_lock:
            return await self._apply(changes)

    async def reconcile(self) -> ApplyResult | None:
        """Full comparison of every group against the whole server."""
        guild = self.guild
        if guild is None:
            log.warning("Guild %s not available; skipping reconcile", self.config.guild_id)
            return None
        if not guild.chunked:
            await guild.chunk()
        member_roles = {m.id: {r.id for r in m.roles} for m in guild.members}
        if not member_roles:
            log.warning("Member list is empty; skipping reconcile")
            return None

        async with self._sync_lock:
            actual = {}
            for g in self.config.groups:
                actual[g.email] = await asyncio.to_thread(self.groups.list_members, g.email)
            changes = plan_full(self.config.groups, member_roles, self.db.all_links(), actual)
            result = await self._apply(changes, max_removals=self.config.max_removals_per_run)

        if changes:
            log.info("Reconcile: %d applied, %d failed, %d held back",
                     len(result.applied), len(result.failed), len(result.skipped_removals))
            await self.post_log(_summary(result, self.config.dry_run))
        return result

    @tasks.loop(minutes=60)
    async def reconcile_loop(self) -> None:
        try:
            await self.reconcile()
        except Exception:
            log.exception("Reconcile failed")

    @reconcile_loop.before_loop
    async def _before_reconcile(self) -> None:
        await self.wait_until_ready()

    async def post_log(self, text: str) -> None:
        if not self.config.log_channel_id:
            return
        channel = self.get_channel(self.config.log_channel_id)
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(text[:2000], suppress_embeds=True)

    async def dm(self, user_id: int, text: str) -> None:
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(text, suppress_embeds=True)
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not DM %s", user_id)

    async def complete_link(self, discord_id: int, email: str) -> tuple[bool, str]:
        """Called by the web callback once Google has confirmed the email."""
        try:
            previous = self.db.set_link(discord_id, email)
        except EmailAlreadyLinked:
            return False, (
                f"{email} is already linked to another Discord account. "
                "Ask an officer if you think this is a mistake."
            )
        await self.sync_member(discord_id, (previous,) if previous else ())

        guild = self.guild
        member = guild.get_member(discord_id) if guild else None
        granted = groups_for_roles(self.config.groups, {r.id for r in member.roles}) if member else []
        text = f"Linked as **{email}**."
        if self.config.dry_run:
            text += " The bot is still in test mode, so access will be granted once it goes live."
        elif granted:
            text += " You now have access to:\n" + _sheet_lines(_sheets(granted))
            text += "\nOpen them while signed in to that Google account. Access can take a few minutes."
        else:
            text += " You don't have a role with sheet access yet; you'll get access automatically when you do."
        await self.dm(discord_id, text)
        return True, f"Your Discord account is now linked to {email}. You can close this tab."

    # --- events ----------------------------------------------------------

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.guild.id != self.config.guild_id:
            return
        before_roles = {r.id for r in before.roles}
        after_roles = {r.id for r in after.roles}
        if before_roles == after_roles:
            return
        email = self.db.get_email(after.id)
        if not email:
            return
        await self.sync_member(after.id)

        had = {s.url for s in _sheets(groups_for_roles(self.config.groups, before_roles))}
        new = [s for s in _sheets(groups_for_roles(self.config.groups, after_roles)) if s.url not in had]
        if new and not self.config.dry_run:
            await self.dm(after.id, "Your new role gives you access to:\n" + _sheet_lines(new))

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild.id == self.config.guild_id and self.db.get_email(member.id):
            await self.sync_member(member.id)

    # --- commands --------------------------------------------------------

    def _is_admin(self, member: discord.Member) -> bool:
        return member.guild_permissions.manage_guild or any(
            r.id in self.config.admin_role_ids for r in member.roles
        )

    def _register_commands(self) -> None:
        @self.tree.command(name="link", description="Link your Google account to get access to faction sheets")
        async def link(interaction: discord.Interaction) -> None:
            state = self.db.create_state(interaction.user.id)
            url = f"{self.config.public_base_url.rstrip('/')}/link?state={state}"
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Sign in with Google", url=url))
            await interaction.response.send_message(
                "Sign in with the Google account you want to open the sheets with. "
                "This link is only for you and expires in 15 minutes.",
                view=view,
                ephemeral=True,
            )

        @self.tree.command(name="unlink", description="Unlink your Google account and remove its sheet access")
        async def unlink(interaction: discord.Interaction) -> None:
            previous = self.db.remove_link(interaction.user.id)
            if not previous:
                await interaction.response.send_message("You have no linked Google account.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.sync_member(interaction.user.id, (previous,))
            await interaction.followup.send(f"Unlinked {previous} and removed its access.", ephemeral=True)

        @self.tree.command(name="status", description="Show your linked Google account and sheet access")
        async def status(interaction: discord.Interaction) -> None:
            email = self.db.get_email(interaction.user.id)
            if not email:
                await interaction.response.send_message(
                    "You haven't linked a Google account yet. Run /link to get started.", ephemeral=True
                )
                return
            roles = {r.id for r in getattr(interaction.user, "roles", [])}
            granted = groups_for_roles(self.config.groups, roles)
            text = f"Linked as **{email}**.\n"
            text += ("Your sheets:\n" + _sheet_lines(_sheets(granted))) if granted else "Your roles don't include any sheets yet."
            await interaction.response.send_message(text, ephemeral=True, suppress_embeds=True)

        @self.tree.command(name="sync", description="Admin: compare all groups with Discord roles now")
        async def sync(interaction: discord.Interaction) -> None:
            if not isinstance(interaction.user, discord.Member) or not self._is_admin(interaction.user):
                await interaction.response.send_message("Only admins can run this.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.reconcile()
            if result is None:
                await interaction.followup.send("Couldn't read the server member list; nothing changed.")
            elif not (result.applied or result.failed or result.skipped_removals):
                await interaction.followup.send("Everything is already in sync.")
            else:
                await interaction.followup.send(_summary(result, self.config.dry_run)[:2000], suppress_embeds=True)
