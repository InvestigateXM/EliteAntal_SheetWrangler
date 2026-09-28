"""Small web server for the "Sign in with Google" linking flow.

/link?state=...           -> redirects to Google's consent screen
/oauth/callback?code=...  -> verifies the Google account and links it
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

if TYPE_CHECKING:
    from .bot import SheetBot

log = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _page(title: str, message: str, status: int = 200) -> web.Response:
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #111418; color: #e8e8e8;
         display: grid; place-items: center; min-height: 100vh; margin: 0; padding: 16px; }}
  main {{ max-width: 32rem; }}
  h1 {{ color: #f0a030; font-size: 1.4rem; }}
</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></main></body></html>"""
    return web.Response(text=body, content_type="text/html", status=status)


def build_app(bot: SheetBot) -> web.Application:
    cfg = bot.config

    async def start(request: web.Request) -> web.StreamResponse:
        state = request.query.get("state", "")
        if not state or bot.db.peek_state(state) is None:
            return _page("Link expired", "This link has expired. Run /link in Discord again.", 400)
        params = {
            "client_id": cfg.oauth_client_id,
            "redirect_uri": cfg.oauth_redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "prompt": "select_account",
        }
        raise web.HTTPFound(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

    async def callback(request: web.Request) -> web.StreamResponse:
        if "error" in request.query:
            return _page("Not linked", "Google sign-in was cancelled. Run /link in Discord to try again.", 400)
        state = request.query.get("state", "")
        code = request.query.get("code", "")
        discord_id = bot.db.consume_state(state) if state else None
        if discord_id is None or not code:
            return _page("Link expired", "This link has expired. Run /link in Discord again.", 400)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": cfg.oauth_client_id,
                    "client_secret": cfg.oauth_client_secret,
                    "redirect_uri": cfg.oauth_redirect_uri,
                    "grant_type": "authorization_code",
                },
            ) as resp:
                token = await resp.json()
        if "id_token" not in token:
            log.warning("Token exchange failed: %s", token.get("error"))
            return _page("Not linked", "Google sign-in failed. Run /link in Discord to try again.", 400)

        try:
            claims = await asyncio.to_thread(
                id_token.verify_oauth2_token,
                token["id_token"],
                google_requests.Request(),
                cfg.oauth_client_id,
            )
        except ValueError:
            log.exception("Invalid ID token")
            return _page("Not linked", "Google sign-in failed. Run /link in Discord to try again.", 400)

        email = claims.get("email")
        if not email or not claims.get("email_verified"):
            return _page("Not linked", "That Google account has no verified email address.", 400)

        ok, message = await bot.complete_link(discord_id, email)
        return _page("Linked" if ok else "Not linked", message, 200 if ok else 409)

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/link", start)
    app.router.add_get("/oauth/callback", callback)
    app.router.add_get("/healthz", health)
    return app
