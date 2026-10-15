# Going live on a Hostinger VPS

This puts the lead router online at **https://app.legalleadrouter.com** on one server:

- **HTTPS:** Caddy gets the certificate from Let's Encrypt and renews it by itself.
- **Firewall:** only SSH, 80 and 443 are open.
- **Backups:** nightly.
- **Soft launch:** the form is online but takes no inquiries until you open a state (see
  [Opening states](#opening-states)).

The WordPress site stays at legalleadrouter.com, and email stays in Hostinger Email.

It takes about 30 minutes. You type every password into the server yourself; none of them go
into GitHub or into a chat.

## What you need

- **A Hostinger VPS** running Ubuntu 24.04. Use the plain Ubuntu template or one with Docker
  already installed; the script installs Docker if it's missing. 4 GB of RAM is plenty for the
  lead router. Choose 8 GB for headroom, and 16 GB if you'll add the eXo workspace later.
  Set a root password or an SSH key when Hostinger asks.
- **The password of the info@legalleadrouter.com mailbox.** The router sends its email
  through it.
- **The business's legal name and postal address.** They appear in the disclosure, the
  privacy policy and every email, and the router refuses to start in production without
  them.
- **A dashboard password** you make up, 12 characters or more.

## 1. Point app.legalleadrouter.com at the server

In hPanel, open the VPS and note its **IPv4 address**. Then go to **Domains → legalleadrouter.com
→ DNS / Nameservers** and add one record:

| Type | Name | Points to | TTL |
|---|---|---|---|
| A | `app` | the VPS's IPv4 address | 300 (or the default) |

- **Leave every other record alone.** The MX and email records keep info@legalleadrouter.com
  working, and the existing A record for the bare domain keeps the WordPress site up.
- **Don't add an AAAA (IPv6) record for `app`.** Over IPv6, every visitor would reach the
  router from the same internal address, so the IP address stored as consent evidence
  would be wrong.

The change usually takes a few minutes. Check it from your own computer: `nslookup
app.legalleadrouter.com` should print the VPS's address.

## 2. Connect to the server

hPanel shows the SSH command on the VPS overview page. From a terminal (PowerShell on
Windows, Terminal on a Mac):

```bash
ssh root@YOUR_VPS_IP
```

hPanel also has a browser terminal if you'd rather not use your own.

## 3. Install

```bash
apt-get update && apt-get install -y git
git clone https://github.com/pat687/MergeME.git /opt/legalleadrouter
cd /opt/legalleadrouter
./deploy/install.sh
```

<details>
<summary>If the GitHub repository is private</summary>

The server needs its own read-only key:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Copy the line it prints into GitHub: **repository → Settings → Deploy keys → Add deploy
key**. Leave "Allow write access" off. Then clone with
`git clone git@github.com:pat687/MergeME.git /opt/legalleadrouter` and carry on.
</details>

The script asks these questions. Press Enter to accept the suggestion in brackets.

| Question | What to type |
|---|---|
| Web address for the app | Enter (`app.legalleadrouter.com`) |
| Business email | Enter (`info@legalleadrouter.com`) |
| Legal name of the business | e.g. `Barrgo LLC`: the exact registered name |
| Business postal address | One line, e.g. `123 Main St, Suite 4, Chicago, IL 60601` |
| Name shown on the form and in emails | Enter keeps `Barrgo Legal Connect`, or type another |
| Password of the mailbox | The info@ mailbox password (it isn't shown as you type) |
| Dashboard login name / password | A login for `/admin`, and a password of 12+ characters |

Then it:

1. installs Docker and turns on the firewall (SSH, 80 and 443 only);
2. writes the answers to `.env`, which only root can read;
3. checks the DNS record from step 1;
4. builds and starts the router, its database and Caddy;
5. checks the site over HTTPS;
6. schedules a nightly backup.

It ends with **"Done. It's live."**, the addresses and the **admin key**. Put the admin key
and the dashboard password in a password manager.

## 4. Check it

- **The form:** https://app.legalleadrouter.com says *"Sorry, we aren't accepting
  inquiries yet."* That's the soft launch working.
- **The dashboard:** https://app.legalleadrouter.com/admin asks for the dashboard login, then
  the admin key.
- **The privacy policy:** https://app.legalleadrouter.com/privacy shows the business name,
  the address and info@legalleadrouter.com.
- **All the checks at once:** on the server, run `./deploy/install.sh status`. Every line
  should say `ok`.

**Send a test email** through the mailbox, to an address of yours:

```bash
cd /opt/legalleadrouter
docker compose exec lead-worker python -c "
from app.config import Settings
from app.services.notifier import SmtpMailer
SmtpMailer(Settings()).send({'to': 'YOU@EXAMPLE.COM', 'subject': 'Test', 'body': 'Email from the server works.'})
print('Sent.')"
```

`Sent.` means Hostinger accepted it. An authentication error means the mailbox password in
`.env` is wrong: run `./deploy/install.sh` again and retype it.

## After the first install

- **Backups:** the database is dumped every night to `/opt/legalleadrouter/backups` and kept
  for 30 days. A copy on the same machine doesn't survive losing the machine, so also:
  - turn on Hostinger's VPS backups or snapshots in hPanel;
  - from time to time, copy the dumps to your computer:
    `scp root@YOUR_VPS_IP:/opt/legalleadrouter/backups/*.gz .`

  Restoring is covered in [RUNBOOK.md](RUNBOOK.md#backups-and-restore).
- **Monitoring:** a free uptime monitor (UptimeRobot, Better Stack and the like) pointed at
  https://app.legalleadrouter.com/readyz emails you if the site or its database stops
  answering.
- **Linking from WordPress:** once a state is open, point a "Get help with a legal matter"
  button at https://app.legalleadrouter.com. The form can also be embedded in a page on
  legalleadrouter.com (an iframe). The server allows that site and no other to frame it.
- **Attorneys** get leads only after staff record a bar-license check for every state they
  take leads in. See [RUNBOOK.md](RUNBOOK.md#attorneys-license-checks-and-the-agreement).

## Opening states

Nothing is sold until a state is open. Open one only after a lawyer licensed there has
approved it in writing ([COMPLIANCE.md](COMPLIANCE.md)). Then, on the server:

```bash
cd /opt/legalleadrouter
nano .env
```

Set the states, for example `ROUTER_LAUNCH_STATES='["IL"]'`.

- **Restricted states or practice areas:** for anything listed in
  `app/domain/compliance.py` (RESTRICTIONS), also add the clearance, e.g.
  `ROUTER_COUNSEL_CLEARED='["IL:personal_injury"]'`. Then record where the written approval
  is filed in `ROUTER_COUNSEL_APPROVAL_REF`. Without that reference the router refuses to
  start.
- **Apply it:** save, then run `docker compose up -d --wait lead-router lead-worker` and
  `./deploy/install.sh status`. The last line lists the open states.

## Updating to the latest version

```bash
cd /opt/legalleadrouter && ./deploy/install.sh update
```

It pulls the latest `main` from GitHub, rebuilds, restarts and checks the site. Your `.env`
and data are kept.

## Changing an answer

Run `./deploy/install.sh` again. Enter keeps each current value, including the passwords.

Changing the name on the form, the legal name or the address gives the consent wording a
new version automatically. Anyone with the old form open is then asked to agree again.

## When something goes wrong

| What you see | What to do |
|---|---|
| "DNS for app.legalleadrouter.com doesn't point to this server yet" | Check the A record from step 1 and wait a few minutes. `nslookup app.legalleadrouter.com` must print the VPS's address |
| "didn't answer within 90 seconds", and Caddy's log mentions a challenge or timeout | Let's Encrypt couldn't reach ports 80/443. If hPanel has a firewall for the VPS, allow TCP 80 and 443 there too |
| "a service didn't start" with "not ready for production" in the log | The message lists the missing setting. Run `./deploy/install.sh` again and fill it in |
| Emails don't arrive | Run the test email above. The dashboard's email queue shows failed messages, and `docker compose logs lead-worker` says why |
| Anything else | `docker compose logs --tail 100 caddy lead-router lead-worker` (or `make logs` to follow them) |

Don't delete the `caddy_data` Docker volume. It holds the certificate, and Let's Encrypt
limits how often one can be requested.

## What this setup does, and what it leaves to you

**Done by `deploy/install.sh` and `deploy/docker-compose.prod.yml`:**

| Area | What it does |
|---|---|
| HTTPS | Automatic renewal and HSTS. Plain `http://` sends visitors to `https://` |
| Staff pages | `/admin`, `/docs` and `/openapi.json` need the dashboard login. The admin API also needs the admin key |
| What's reachable | The router, the database and Mailpit aren't published. The firewall allows only SSH, 80 and 443 |
| Visitor IP | The router stores the visitor's real IP address as consent evidence and ignores a forged `X-Forwarded-For` (it trusts that header from Caddy's fixed address only) |
| Production mode | Demo mode off, license checks on, real SMTP over SSL. The router itself refuses to start without the legal entity name and address |
| Housekeeping | Container logs are capped. `.env` and the backups can be read by root only |

**Still yours** (see the production checklist in [RUNBOOK.md](RUNBOOK.md#going-to-production)):

- **Sign-in:** single sign-on with two-factor authentication in front of `/admin`. The
  password login is the minimum.
- **Backups:** copies off the server.
- **Monitoring:** an uptime monitor, as above.
- **Launch:** the legal sign-off before opening a state.
