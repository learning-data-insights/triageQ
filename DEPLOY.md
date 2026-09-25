# Hosting triageQ on AWS

A step-by-step guide to running the web build of triageQ (`web_app.py`) as a
public reference deployment on a single **Amazon Lightsail** server, with HTTPS.

**What you end up with:** `https://triageq.<your-domain>` serving triageQ in the
browser. Visitors get a private, temporary workspace. They either paste their
own API key or use your organisation's capped demo key.

**Rough cost:** the Lightsail server is a fixed monthly price (the 2 GB plan,
about $12/month at the time of writing; check the price Lightsail shows you),
plus automatic snapshots (cents per month). Demo-key model calls are billed
separately by Anthropic/OpenAI, up to whatever spending limit you set there.

**Time:** about an hour the first time, most of it waiting for DNS.

---

## Contents

- [How the hosted build works](#how-the-hosted-build-works)
- [Step 0 — Try it on your own computer](#step-0--try-it-on-your-own-computer)
- [Step 1 — Prepare the demo API keys](#step-1--prepare-the-demo-api-keys)
- [Step 2 — AWS account and a budget alarm](#step-2--aws-account-and-a-budget-alarm)
- [Step 3 — Create the Lightsail server](#step-3--create-the-lightsail-server)
- [Step 4 — Static IP and firewall](#step-4--static-ip-and-firewall)
- [Step 5 — Point your domain at the server](#step-5--point-your-domain-at-the-server)
- [Step 6 — Install Docker](#step-6--install-docker)
- [Step 7 — Get the code and configure it](#step-7--get-the-code-and-configure-it)
- [Step 8 — Start it](#step-8--start-it)
- [Step 9 — Lock down the metadata address](#step-9--lock-down-the-metadata-address)
- [Step 10 — Snapshots](#step-10--snapshots)
- [Day-to-day operation](#day-to-day-operation)
- [Troubleshooting](#troubleshooting)
- [Taking it down](#taking-it-down)

---

## How the hosted build works

```
 visitor's browser ──HTTPS──▶ Caddy (ports 80/443, auto certificate)
                                  │
                                  ▼
                           triageQ app container (Streamlit, port 8501)
                                  │
              ┌───────────────────┼─────────────────────────┐
              ▼                   ▼                         ▼
   /data/workspaces/<id>/   PDF lookups (Unpaywall,    Anthropic / OpenAI
   one folder per visitor   OpenAlex, publishers…)     (visitor's key or demo key)
```

**Workspaces.** Each visitor gets a random workspace id, carried in the page URL
(`?ws=…`). Bookmarking the URL brings them back to their work, and anyone who
has the link can open it. A workspace holds that visitor's criteria profiles,
uploads and results.

**Persistence.** Nothing is kept for long:

| What | Kept where | Deleted when |
|------|-----------|--------------|
| A visitor's own API key | Server memory for that browser session only; never on disk | Tab closed or page refreshed |
| Profiles, uploads, results | `/data/workspaces/<id>/` (a Docker volume) | After `TRIAGEQ_WORKSPACE_TTL_HOURS` (default 24) without use, or right away with **Clear my session** |
| Demo-key usage counters | `/data/usage/<date>.json` | After 14 days |

The app checks for expired workspaces every 30 minutes, so there's no cron job
to set up. Visitors keep their work with **Download all results (.zip)**.

**Limits** are all in `.env`: papers per batch, MB per file, MB per workspace,
how many screenings run at once across the server (the rest wait in a queue),
and a minimum free-disk threshold below which uploads are refused.

**Demo key.** If you put an organisation key in `.env`, visitors see a *"Use the
demo key"* option. It always uses the model you chose, and it's capped per day
for everyone combined and per visitor. The per-visitor cap counts both the
workspace and the visitor's IP address, so clearing the session doesn't reset
it. These caps are a courtesy limit. **The real ceiling is the spending limit
you set in the provider's console (Step 1).**

**Safety.** The server fetches links that visitors supply, so every outbound
URL, including each redirect, is checked against private and internal
addresses (`net_guard.py`). Step 9 adds a firewall rule as a second layer.

---

## Step 0 — Try it on your own computer

You need Docker Desktop (already installed on this machine). From the repo folder:

```bash
cp .env.example .env          # then edit .env: at least TRIAGEQ_CONTACT_EMAIL
docker build -t triageq-web .
docker run --rm -p 8501:8501 --env-file .env triageq-web
```

Open <http://localhost:8501>. Click through the app: **Criteria → Load example**,
**Screen one paper** with your own key, then **Clear my session**. Press
`Ctrl+C` in the terminal to stop it.

> Caddy isn't used locally. It only runs on the server, where there's a real domain.

---

## Step 1 — Prepare the demo API keys

Skip this step if you'll only offer "bring your own key".

For each provider you want to offer as a demo:

1. **Create a dedicated key just for this server.** Don't reuse a personal or
   production key, so you can revoke this one without breaking anything else.
   - Anthropic: console.anthropic.com → create a Workspace (for example
     `triageq-demo`) → API Keys → Create key in that workspace
   - OpenAI: platform.openai.com → create a Project (for example `triageq-demo`) →
     API keys → Create key in that project
2. **Set a hard monthly spending limit** on that workspace/project, for example
   $25. This is the backstop if anything goes wrong with the in-app caps.
3. Keep the key somewhere safe for Step 7. You'll paste it into the server's
   `.env`, nowhere else.

---

## Step 2 — AWS account and a budget alarm

1. Sign in to <https://console.aws.amazon.com>, or create an account.
2. **Turn on MFA for the root user:** top-right account menu → *Security credentials* →
   *Assign MFA device*.
3. **Set a budget alarm:** search *Budgets* → *Create budget* → *Use a template* →
   *Monthly cost budget* → amount `20` → your email → *Create*. AWS will email you
   if spending heads past $20.

---

## Step 3 — Create the Lightsail server

1. Open <https://lightsail.aws.amazon.com> and click **Create instance**.
2. **Region:** pick one close to your users (for example *Virginia, us-east-1*).
3. **Select a platform:** **Linux operating system**. Not *Linux apps*, which is
   selected by default and preinstalls software like WordPress that would get in
   the way. **Select a blueprint:** **Ubuntu 24.04 LTS**.
4. **Networking type:** **Dual-stack**, not IPv6-only. The domain setup in
   Step 5 needs an IPv4 address.
5. **Plan:** choose the **2 GB RAM** plan. 1 GB is too tight once a few PDFs are
   being processed at the same time.
6. **Name:** `triageq`. Click **Create instance**, then wait about a minute for
   it to show *Running*.

---

## Step 4 — Static IP and firewall

A static IP keeps the server's address the same across reboots.

1. Open the instance → **Networking** tab.
2. **Attach static IP:** click *Attach static IP* → name it `triageq-ip` →
   *Create*. Note the address, for example `3.91.12.34`. It's free while it's
   attached to a running instance.
3. **Firewall (IPv4):** the defaults are SSH (22) and HTTP (80). Click
   **+ Add rule** → Application **HTTPS** → *Create*. You now have 22, 80 and 443.
4. Do the same under the **IPv6 firewall** if it's shown.

> Optional hardening: on the SSH rule, tick *Restrict to IP address* and
> *Allow Lightsail browser SSH*, so only you and the browser console can connect.

---

## Step 5 — Point your domain at the server

Wherever your domain's DNS is managed (your registrar, Cloudflare, Route 53, …),
add one record:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A | `triageq` | the static IP from Step 4 | 300 (or the default) |

That makes `triageq.<your-domain>` point at the server. It can take from a few
minutes to an hour to take effect. To check from your computer:

```bash
nslookup triageq.<your-domain>
```

When it returns the static IP, you're ready. If you use Cloudflare, set the
record to **DNS only** (grey cloud) so Caddy can get its certificate.

---

## Step 6 — Install Docker

1. On the instance page, click **Connect using SSH**. A terminal opens in the browser.
2. Paste each block in turn:

```bash
# System updates
sudo apt update && sudo apt -y upgrade

# Docker Engine + Compose plugin (Docker's official install script)
curl -fsSL https://get.docker.com | sudo sh

# Check
sudo docker --version && sudo docker compose version
```

3. **Add swap** (a safety margin so a memory spike slows the server down instead
   of crashing the app):

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## Step 7 — Get the code and configure it

1. **Clone the repository** (still in the SSH terminal):

```bash
cd ~
git clone https://github.com/learning-data-insights/triageQ.git
cd triageQ
```

   The repository is public, so no GitHub login is needed.

2. **Create `.env`** from the template and edit it:

```bash
cp .env.example .env
chmod 600 .env              # only you can read it — it holds the demo keys
nano .env
```

   At minimum, set:

   - `TRIAGEQ_DOMAIN=triageq.<your-domain>` (exactly the name from Step 5)
   - `TRIAGEQ_CONTACT_EMAIL=` an address you own (used for Unpaywall/OpenAlex lookups)
   - optionally the `TRIAGEQ_HOSTED_*` demo keys from Step 1, and their models
     (the OpenAI model is required if you set an OpenAI key)

   Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

---

## Step 8 — Start it

```bash
sudo docker compose up -d --build
```

The first build takes a few minutes. Then:

```bash
sudo docker compose ps                 # both services should be "running"/"healthy"
sudo docker compose logs -f caddy      # watch for "certificate obtained successfully"
```

Press `Ctrl+C` to stop following the logs; the app keeps running. Open
`https://triageq.<your-domain>`, and you should see triageQ with a padlock in
the address bar.

**Smoke test:** Criteria → *Load example* → Screen one paper → paste
`10.1371/journal.pone.0115069` → *Find PDF* → pick the demo key (or paste your
own) → *Analyze paper*.

Both containers restart on their own after a crash or a server reboot
(`restart: unless-stopped`).

---

## Step 9 — Lock down the metadata address

Every server on AWS can reach an internal "metadata" address (169.254.169.254).
The app already refuses to fetch it. This rule makes sure no container can reach
it at all, whatever the app does:

```bash
sudo iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
# Re-apply after every reboot (Docker rebuilds its firewall chain on start):
( sudo crontab -l 2>/dev/null; echo '@reboot sleep 30 && /usr/sbin/iptables -I DOCKER-USER -d 169.254.169.254 -j DROP' ) | sudo crontab -
```

To check it works (this should time out rather than answer):

```bash
sudo docker compose exec app python -c "import urllib.request; urllib.request.urlopen('http://169.254.169.254/', timeout=3)"
```

---

## Step 10 — Snapshots

Instance → **Snapshots** tab → turn on **Automatic snapshots**. Lightsail keeps a
daily image of the whole server, which is useful if an update goes wrong. The
workspaces are temporary anyway, so this is really a backup of your setup
(`.env`, certificates, configuration).

---

## Day-to-day operation

All commands run in `~/triageQ` over SSH.

| Task | Command |
|------|---------|
| Deploy an update | `git pull && sudo docker compose up -d --build` |
| Change a limit or key | `nano .env` then `sudo docker compose up -d` |
| See app logs | `sudo docker compose logs --tail 100 app` |
| Restart | `sudo docker compose restart app` |
| Today's demo-key usage | `sudo docker compose exec app sh -c 'cat /data/usage/$(date -u +%F).json'` |
| How many workspaces exist | `sudo docker compose exec app sh -c 'ls /data/workspaces \| wc -l'` |
| Purge expired workspaces now | `sudo docker compose exec app python web_backend.py cleanup` |
| **Wipe every workspace now** | `sudo docker compose exec app sh -c 'rm -rf /data/workspaces/*'` |
| Disk space | `df -h /` |

**Want a hard wipe every night**, whether or not a workspace is in use? Add a
cron job on the server (this example runs at 04:00 UTC):

```bash
( sudo crontab -l 2>/dev/null; echo "0 4 * * * cd /home/ubuntu/triageQ && /usr/bin/docker compose exec -T app sh -c 'rm -rf /data/workspaces/*'" ) | sudo crontab -
```

Otherwise the built-in rule (deleted after 24 h without use) is usually enough.

**Keep an eye on:** the provider consoles' usage pages (demo-key spend), the AWS
budget email, and `df -h` now and then.

**Rotating a demo key:** create a new key in the provider console, put it in
`.env`, run `sudo docker compose up -d`, then revoke the old key.

---

## Troubleshooting

**The site doesn't load / no padlock.** Run `sudo docker compose logs caddy`.
Most often DNS isn't pointing at the static IP yet (`nslookup` again), or port
443 isn't open in the Lightsail firewall (Step 4). Caddy retries on its own, so
once the cause is fixed, wait a minute.

**"502 Bad Gateway".** The app container isn't up. Check
`sudo docker compose ps` and `sudo docker compose logs app`.

**Uploads fail for big files.** The per-file limit is `TRIAGEQ_MAX_UPLOAD_MB`
(default 25). Raise it in `.env` and run `sudo docker compose up -d`.

**"The server is busy".** All `TRIAGEQ_MAX_CONCURRENT_JOBS` slots are in use.
Raise it (3–4 is comfortable on 2 GB), or move to a larger plan (Lightsail →
Snapshots → create a new, bigger instance from a snapshot).

**A known open-access paper isn't found.** Some publishers block automated
downloads from cloud servers. That's expected. The visitor can upload the PDF
instead.

**Visitors lose their work after a refresh.** They shouldn't lose results. The
workspace id is in the URL, so refreshing keeps it. Their **API key** is
forgotten on refresh on purpose, and they need to paste it again.

---

## Taking it down

1. Lightsail → instance → **Delete**.
2. Lightsail → Networking → **release the static IP** (an unattached static IP is
   billed).
3. Delete the automatic snapshots.
4. Remove the DNS record.
5. Revoke the demo API keys in the provider consoles.
