# EliteAntal SheetWrangler

A Discord bot that controls who can open the faction's Google Sheets based on
their Discord roles. Nobody gets added to a sheet by hand, and people who lose
a role or leave the server lose access automatically.

## How it works

- Each Discord role that should unlock sheets maps to a **Google group** on our
  domain (for example `pledged@yourdomain`). Each sheet is shared **once** with
  its group.
- Players run `/link` once and sign in with Google. This proves which Google
  account is theirs.
- The bot adds that Google account to the groups matching the player's roles.
  When roles change, or the player leaves the server, the bot updates the
  groups within seconds.
- Once an hour (configurable) the bot compares every group with the whole
  server and fixes any differences, including anything it missed while it was
  offline.

Sheets stay owned by whoever owns them now, and players keep using their own
Gmail accounts.

### Player commands

| Command   | What it does |
|-----------|--------------|
| `/link`   | Sign in with Google to link your account (run again to switch accounts) |
| `/status` | Shows your linked account and the sheets you have |
| `/unlink` | Removes the link and all sheet access |

### Admin command

| Command | What it does |
|---------|--------------|
| `/sync` | Runs the full comparison now and reports the changes. Requires Manage Server or a role listed in `admin_roles`. |

## One-time setup

### 1. Cloud Identity Free on your domain

1. Sign up at <https://workspace.google.com/gcpidentity/signup?sku=identitybasic>
   with the domain you already use for the wiki. Verify it with the DNS TXT
   record Google gives you. This doesn't change your existing email or website.
2. In the Admin console (<https://admin.google.com>), go to
   **Apps > Google Workspace > Groups for Business > Sharing settings** and turn
   on **Group owners can allow external members**.
3. Create one group per role (**Directory > Groups > Create group**), for
   example `pledged@yourdomain`. In each group's **Access settings**, turn on
   **Allow external members**, and set who can see and post to it to owners
   only, since these groups are only for permissions.
4. In each Google Sheet, click **Share** and add the group's address as Viewer or
   Editor. For sensitive sheets, open the gear in the share dialog and turn off
   **Viewers and commenters can see the option to download, print and copy**.

### 2. Google Cloud project

1. Create a project at <https://console.cloud.google.com> and enable the
   **Admin SDK API**.
2. **Service account:** go to **IAM & Admin > Service accounts** and create one,
   then create a JSON key and save it as `service-account.json` next to the bot.
   In the Admin console, go to **Account > Admin roles > Groups Admin > Assign
   service accounts** and add the service account's email.
3. **Consent screen:** go to **Google Auth Platform > Branding / Audience**.
   Choose External, fill in the app name, and **publish** the app. It only asks
   for `openid` and `email`, which don't need Google verification.
4. **OAuth client:** go to **Clients > Create client > Web application**. Add
   `https://<your bot address>/oauth/callback` as an authorized redirect URI.
   Put the client ID in `config.yaml` and the secret in `.env`.

### 3. Discord application

1. Create an application at <https://discord.com/developers/applications> and add
   a bot.
2. Under **Bot**, turn on **Server Members Intent**, which the bot needs to see
   role changes and people leaving. Copy the token into `.env`.
3. Invite the bot with the `bot` and `applications.commands` scopes. It needs no
   special permissions beyond sending messages in the log channel.

### 4. Run it

```sh
cp config.example.yaml config.yaml   # fill in IDs and groups
cp .env.example .env                 # fill in secrets
mkdir -p data
docker compose up -d --build
```

The bot listens on port 8080 and needs a public HTTPS address for the Google
sign-in redirect. Put it behind the same reverse proxy as Authentik, for
example `sheets.yourdomain` pointing to `127.0.0.1:8080`.

Without Docker, run `pip install -r requirements.txt`, then
`python -m antal_sheets --config config.yaml`.

### 5. Go live safely

The bot starts with `dry_run: true`. In that mode it logs, and posts to
`log_channel_id`, what it **would** change without touching the groups.

1. Ask a few people to `/link`, then run `/sync` and read the report.
2. Add anyone who must never be removed, such as sheet owners, to that group's
   `protected` list. Owners and managers of a group are never removed anyway.
3. Set `dry_run: false` and restart.

If one sync ever plans to remove more than `max_removals_per_run` people, the
removals are held back and reported instead. This protects against a Discord
outage or a config mistake emptying a group.

## What players need to do

1. Get the role in Discord, the same way as today.
2. Run `/link`, press **Sign in with Google**, and pick the Google account they
   use for the sheets.
3. Open the sheet links the bot DMs them, signed in to that same Google
   account. If Google shows "Request access", they're on the wrong account.

Players without Gmail can create a Google account for their existing email
address at <https://accounts.google.com/signup>. To do that, choose **Use my
current email address instead**.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
