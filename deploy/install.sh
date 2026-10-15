#!/usr/bin/env bash
# Put the lead router online on one Ubuntu server, behind HTTPS. See docs/DEPLOY.md.
#
#   sudo ./deploy/install.sh           first install, or change the answers it asks for
#   sudo ./deploy/install.sh update    get the latest code from GitHub and restart
#        ./deploy/install.sh status    what's running, and whether the site answers
#
# Safe to run again. It keeps .env and only changes the values it manages. Passwords are
# typed here and written only to .env, which only root can read. Nothing is sold until
# ROUTER_LAUNCH_STATES lists a state (a soft launch), and that stays your decision.
#
# Unattended runs read the answers from environment variables with the same names
# (DEPLOY_NONINTERACTIVE=1). DEPLOY_SKIP_SYSTEM=1 skips the system setup (packages, Docker,
# firewall, backup schedule), and DEPLOY_LOCAL_CERTS=1 uses a private test certificate
# instead of Let's Encrypt, and DEPLOY_SKIP_BUILD=1 uses an already built router image.
# Those three are for testing. Answers: ROUTER_DOMAIN, BUSINESS_EMAIL,
# ROUTER_LEGAL_ENTITY_NAME, ROUTER_POSTAL_ADDRESS, ROUTER_PLATFORM_NAME, SMTP_PASSWORD,
# STAFF_USER and STAFF_PASSWORD.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
SERVICES=(router-db lead-router lead-worker caddy)
CADDY_IMAGE=$(sed -n -E 's/^[[:space:]]*image:[[:space:]]*(caddy:[^[:space:]]+).*/\1/p' deploy/docker-compose.prod.yml)
COMPOSE_FILES="docker-compose.yml:deploy/docker-compose.prod.yml"
# The values .env.example ships for the demo. Production refuses the clearance without an
# approval reference, so finding them unchanged means nobody has launched anything yet.
DEMO_LAUNCH_STATES='["IL"]'
DEMO_COUNSEL_CLEARED='["IL:personal_injury","IL:medical_malpractice","IL:workers_comp"]'

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
fail() {
  printf '\n\033[31mStopped:\033[0m %s\n' "$*" >&2
  exit 1
}
interactive() { [[ ${DEPLOY_NONINTERACTIVE:-0} != 1 && -t 0 ]]; }

# --- .env -------------------------------------------------------------------------
# The value of KEY in .env, without the quotes this script or `make init` writes.
env_get() {
  local line
  line=$(grep -E "^$1=" .env | tail -n 1) || return 0
  line=${line#*=}
  if [[ ${#line} -ge 2 && $line == \'*\' ]]; then
    line=${line:1:${#line}-2}
  elif [[ ${#line} -ge 2 && $line == \"*\" ]]; then
    line=${line:1:${#line}-2}
    line=${line//\\\"/\"}
    line=${line//\\\\/\\}
  fi
  printf '%s' "$line"
}

# Set KEY to VALUE in .env (replacing every earlier line for KEY), quoted so Docker Compose
# and the shell both read it back exactly: single quotes, or double quotes when the value
# has an apostrophe ("O'Brien Law LLC").
env_set() {
  local key=$1 value=$2 line
  [[ $value == *$'\n'* ]] && fail "$key must fit on one line."
  if [[ -z $value ]]; then
    line="$key="
  elif [[ $value != *"'"* ]]; then
    line="$key='$value'"
  else
    [[ $value == *'$'* || $value == *'`'* ]] && fail "$key can't contain both an apostrophe and \$ or \`."
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    line="$key=\"$value\""
  fi
  LINE=$line awk -v key="$key" '
    index($0, key "=") == 1 { if (!done) { print ENVIRON["LINE"]; done = 1 } next }
    { print }
    END { if (!done) print ENVIRON["LINE"] }
  ' .env >.env.tmp
  chmod 600 .env.tmp
  mv .env.tmp .env
}

# ask VAR "Question" default [secret]: the answer goes into the shell variable VAR. An
# unattended run takes VAR from the environment, then the default.
ask() {
  local var=$1 question=$2 default=${3:-} secret=${4:-} answer
  if ! interactive; then
    answer=${!var:-$default}
  elif [[ -n $secret ]]; then
    read -r -s -p "  $question${default:+ [press Enter to keep the current one]}: " answer
    echo
    answer=${answer:-$default}
  else
    read -r -p "  $question${default:+ [$default]}: " answer
    answer=${answer:-$default}
  fi
  printf -v "$var" '%s' "$answer"
}

# --- System -------------------------------------------------------------------------
system_setup() {
  [[ $EUID -eq 0 ]] || fail "run it as root: sudo ./deploy/install.sh"
  say "1/5  System packages, Docker and the firewall"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git make curl ca-certificates ufw openssl >/dev/null
  if ! command -v docker >/dev/null 2>&1; then
    note "Installing Docker (official script from get.docker.com)..."
    curl -fsSL https://get.docker.com | sh >/dev/null
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true

  # Keep SSH open, on every port sshd listens on, before turning the firewall on.
  local ssh_ports port
  ssh_ports=$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' | sort -u | tr '\n' ' ')
  for port in ${ssh_ports:-22}; do ufw allow "$port/tcp" >/dev/null; done
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 443/udp >/dev/null
  ufw --force enable >/dev/null
  note "Firewall: only SSH (port ${ssh_ports:-22}), 80 and 443 are open."
}

check_compose() {
  docker compose version >/dev/null 2>&1 || fail "Docker Compose isn't installed (docker compose version failed)."
  # `ports: !reset []` in deploy/docker-compose.prod.yml needs Compose 2.24.4 or newer.
  local v
  v=$(docker compose version --short | sed 's/^v//')
  if [[ $(printf '%s\n%s\n' 2.24.4 "$v" | sort -V | head -n 1) != 2.24.4 ]]; then
    fail "Docker Compose $v is too old; 2.24.4 or newer is needed (apt-get install docker-compose-plugin)."
  fi
}

backup_schedule() {
  cat >/etc/cron.d/legalleadrouter-backup <<EOF
# Nightly database backup (make backup), keeping 30 days. Written by deploy/install.sh.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
15 3 * * * root cd $ROOT && umask 077 && make backup 2>&1 | logger -t legalleadrouter-backup && find backups -name 'router-*.sql.gz' -mtime +30 -delete
EOF
  chmod 644 /etc/cron.d/legalleadrouter-backup
  note "Nightly backup at 03:15 server time into $ROOT/backups (kept 30 days)."
}

# --- Settings -----------------------------------------------------------------------
configure() {
  say "2/5  Settings"
  local created=0
  if [[ ! -f .env ]]; then
    ./scripts/init-env.sh >/dev/null
    created=1
    note "Created .env with new random secrets."
  fi
  chmod 600 .env

  local current_domain current_email
  current_domain=$(env_get ROUTER_DOMAIN)
  current_email=$(env_get ROUTER_PRIVACY_EMAIL)
  ask ROUTER_DOMAIN "Web address for the app (its DNS A record must point to this server)" \
    "${current_domain:-app.legalleadrouter.com}"
  ROUTER_DOMAIN=${ROUTER_DOMAIN,,}
  [[ $ROUTER_DOMAIN =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] ||
    fail "'$ROUTER_DOMAIN' isn't a domain name like app.legalleadrouter.com."
  ask BUSINESS_EMAIL "Business email (sends the emails, receives privacy requests)" \
    "${current_email:-info@legalleadrouter.com}"
  [[ $BUSINESS_EMAIL =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] ||
    fail "'$BUSINESS_EMAIL' isn't an email address."
  ask ROUTER_LEGAL_ENTITY_NAME "Legal name of the business (shown in the disclosure and emails)" \
    "$(env_get ROUTER_LEGAL_ENTITY_NAME)"
  [[ -n $ROUTER_LEGAL_ENTITY_NAME ]] || fail "the legal name of the business is required."
  ask ROUTER_POSTAL_ADDRESS "Business postal address (one line)" "$(env_get ROUTER_POSTAL_ADDRESS)"
  [[ -n $ROUTER_POSTAL_ADDRESS ]] || fail "a postal address is required."
  ask ROUTER_PLATFORM_NAME "Name shown on the form and in emails" \
    "$(env_get ROUTER_PLATFORM_NAME)"
  ROUTER_PLATFORM_NAME=${ROUTER_PLATFORM_NAME:-Barrgo Legal Connect}
  ask SMTP_PASSWORD "Password of the $BUSINESS_EMAIL mailbox" \
    "$(env_get ROUTER_SMTP_PASSWORD)" secret
  [[ -n $SMTP_PASSWORD ]] || fail "the mailbox password is required to send email."

  # Dashboard login (in front of /admin and /docs).
  local staff_hash
  staff_hash=$(env_get STAFF_PASSWORD_HASH)
  ask STAFF_USER "Dashboard login name" "$(env_get STAFF_USER)"
  STAFF_USER=${STAFF_USER:-admin}
  [[ $STAFF_USER =~ ^[A-Za-z0-9._-]+$ ]] || fail "use letters, digits, dot, dash or underscore in the login name."
  if interactive; then
    local pw pw2
    while :; do
      read -r -s -p "  Dashboard password (12+ characters)${staff_hash:+ [press Enter to keep the current one]}: " pw
      echo
      [[ -z $pw && -n $staff_hash ]] && break
      if ((${#pw} < 12)); then
        note "Too short; use at least 12 characters."
        continue
      fi
      read -r -s -p "  Same password again: " pw2
      echo
      [[ $pw == "$pw2" ]] && {
        staff_hash=$(printf '%s\n' "$pw" | docker run --rm -i "$CADDY_IMAGE" caddy hash-password)
        break
      }
      note "They don't match; try again."
    done
  elif [[ -n ${STAFF_PASSWORD:-} ]]; then
    ((${#STAFF_PASSWORD} >= 12)) || fail "STAFF_PASSWORD needs at least 12 characters."
    staff_hash=$(printf '%s\n' "$STAFF_PASSWORD" | docker run --rm -i "$CADDY_IMAGE" caddy hash-password)
  fi
  [[ $staff_hash == \$2* ]] || fail "a dashboard password is required (STAFF_PASSWORD when unattended)."

  # Mail goes through Hostinger on port 465, unless .env (or the environment) names
  # another server. The password must never travel unencrypted.
  local smtp_host smtp_port smtp_ssl smtp_starttls smtp_from
  smtp_host=${ROUTER_SMTP_HOST:-$(env_get ROUTER_SMTP_HOST)}
  if [[ -z $smtp_host || $smtp_host == mailpit ]]; then
    smtp_host=smtp.hostinger.com smtp_port=465 smtp_ssl=true smtp_starttls=
  else
    smtp_port=${ROUTER_SMTP_PORT:-$(env_get ROUTER_SMTP_PORT)}
    smtp_ssl=${ROUTER_SMTP_SSL-$(env_get ROUTER_SMTP_SSL)}
    smtp_starttls=${ROUTER_SMTP_STARTTLS-$(env_get ROUTER_SMTP_STARTTLS)}
  fi
  [[ ${smtp_ssl,,} == true || ${smtp_starttls,,} == true ]] ||
    fail "$smtp_host: set ROUTER_SMTP_SSL=true (port 465) or ROUTER_SMTP_STARTTLS=true (port 587), so the password is encrypted."
  # A bare address is shown under the platform name; "Name <address>" is kept as written.
  smtp_from=$(env_get ROUTER_SMTP_FROM)
  [[ $smtp_from == *"<"* ]] || smtp_from=$BUSINESS_EMAIL

  # The wording on the form names the platform and the business. When it changes on a
  # running site, a new version makes anyone with the old form open agree again.
  if ((!created)) && [[ "$(env_get ROUTER_PLATFORM_NAME)|$(env_get ROUTER_LEGAL_ENTITY_NAME)|$(env_get ROUTER_POSTAL_ADDRESS)" != "$ROUTER_PLATFORM_NAME|$ROUTER_LEGAL_ENTITY_NAME|$ROUTER_POSTAL_ADDRESS" ]]; then
    env_set ROUTER_DISCLOSURE_VERSION "$(date -u +%Y-%m-%d.%H%M)"
    note "The name or address changed, so the consent wording has a new version."
  fi

  # The marketing site (allowed to show the form in a frame): app.example.com -> example.com.
  local site=${SITE_DOMAIN:-$(env_get SITE_DOMAIN)}
  if [[ -z $site ]]; then
    site=$ROUTER_DOMAIN
    [[ $ROUTER_DOMAIN == *.*.* ]] && site=${ROUTER_DOMAIN#*.}
  fi

  env_set COMPOSE_FILE "$COMPOSE_FILES"
  env_set ROUTER_DOMAIN "$ROUTER_DOMAIN"
  env_set SITE_DOMAIN "$site"
  env_set ACME_EMAIL "$BUSINESS_EMAIL"
  env_set STAFF_USER "$STAFF_USER"
  env_set STAFF_PASSWORD_HASH "$staff_hash"
  env_set ROUTER_PUBLIC_BASE_URL "https://$ROUTER_DOMAIN"
  env_set ROUTER_DEMO_MODE false
  env_set ROUTER_REQUIRE_LICENSE_VERIFICATION true
  env_set ROUTER_LEGAL_ENTITY_NAME "$ROUTER_LEGAL_ENTITY_NAME"
  env_set ROUTER_POSTAL_ADDRESS "$ROUTER_POSTAL_ADDRESS"
  env_set ROUTER_PRIVACY_EMAIL "$BUSINESS_EMAIL"
  env_set ROUTER_PLATFORM_NAME "$ROUTER_PLATFORM_NAME"
  env_set ROUTER_SMTP_HOST "$smtp_host"
  env_set ROUTER_SMTP_PORT "$smtp_port"
  env_set ROUTER_SMTP_SSL "$smtp_ssl"
  env_set ROUTER_SMTP_STARTTLS "$smtp_starttls"
  env_set ROUTER_SMTP_USERNAME "${ROUTER_SMTP_USERNAME:-$BUSINESS_EMAIL}"
  env_set ROUTER_SMTP_PASSWORD "$SMTP_PASSWORD"
  env_set ROUTER_SMTP_FROM "$smtp_from"
  # Informational: deploy/docker-compose.prod.yml sets the router's trusted proxy itself.
  env_set ROUTER_TRUSTED_PROXY_IPS 10.203.57.10
  if [[ ${DEPLOY_LOCAL_CERTS:-0} == 1 ]]; then
    env_set CADDY_GLOBAL_OPTIONS local_certs
  else
    env_set CADDY_GLOBAL_OPTIONS ""
  fi

  # Soft launch: clear the demo's Illinois values. Real launch settings are left alone.
  if ((created)) || [[ $(env_get ROUTER_LAUNCH_STATES) == "$DEMO_LAUNCH_STATES" &&
    $(env_get ROUTER_COUNSEL_CLEARED) == "$DEMO_COUNSEL_CLEARED" &&
    -z $(env_get ROUTER_COUNSEL_APPROVAL_REF) ]]; then
    env_set ROUTER_LAUNCH_STATES ""
    env_set ROUTER_COUNSEL_CLEARED ""
  fi
  local launch
  launch=$(env_get ROUTER_LAUNCH_STATES)
  if [[ -z $launch || $launch == "[]" ]]; then
    note "Soft launch: the form is online but accepts no inquiries until ROUTER_LAUNCH_STATES"
    note "lists a state counsel has approved (docs/DEPLOY.md, \"Opening states\")."
  else
    note "Open states (ROUTER_LAUNCH_STATES): $launch"
  fi
}

# --- DNS ----------------------------------------------------------------------------
check_dns() {
  [[ ${DEPLOY_LOCAL_CERTS:-0} == 1 ]] && return 0
  say "3/5  DNS"
  local domain public resolved
  domain=$(env_get ROUTER_DOMAIN)
  public=$(curl -fsS4 --max-time 10 https://api.ipify.org || true)
  resolved=$(getent ahostsv4 "$domain" | awk '{print $1; exit}' || true)
  if [[ -n $public && $resolved == "$public" ]]; then
    note "$domain points to this server ($public)."
    return 0
  fi
  note "$domain points to ${resolved:-nothing yet}, but this server is ${public:-unknown}."
  note "Add an A record for it in Hostinger (hPanel > Domains > DNS) pointing to ${public:-the IP address of this server},"
  note "wait a few minutes, then run this again. Until it points here, HTTPS can't be set up."
  if interactive; then
    local go
    read -r -p "  Start anyway? [y/N]: " go
    [[ $go == [yY]* ]] && return 0
  fi
  fail "DNS for $domain doesn't point to this server yet."
}

# --- Start and check ----------------------------------------------------------------
start() {
  say "4/5  Building and starting"
  check_compose
  docker compose pull -q --ignore-pull-failures caddy router-db
  local build=(--build)
  [[ ${DEPLOY_SKIP_BUILD:-0} == 1 ]] && build=()
  if ! docker compose up -d "${build[@]}" --wait --remove-orphans "${SERVICES[@]}"; then
    docker compose logs --tail 40 lead-router lead-worker caddy >&2 || true
    fail "a service didn't start. The last log lines above say why (\"not ready for production\" lists missing settings)."
  fi
}

verify() {
  say "5/5  Checking the site"
  local domain url test_cert=0
  domain=$(env_get ROUTER_DOMAIN)
  url="https://$domain/readyz"
  # Ask Caddy on this machine directly, checking its certificate for the real name. A
  # Let's Encrypt certificate can only have been issued if the internet reached ports 80
  # and 443 here, so this also covers DNS and the firewall.
  local opts=(--noproxy '*' --resolve "$domain:443:127.0.0.1")
  if [[ $(env_get CADDY_GLOBAL_OPTIONS) == *local_certs* ]]; then
    opts+=(--insecure)
    test_cert=1
  fi
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "${opts[@]}" "$url" >/dev/null 2>&1; then
      if ((test_cert)); then
        note "https://$domain answers (test certificate)."
      else
        note "https://$domain answers, with a valid certificate."
      fi
      return 0
    fi
    sleep 3
  done
  docker compose logs --tail 30 caddy lead-router >&2 || true
  fail "https://$domain didn't answer within 90 seconds. The logs above usually say why (DNS, ports 80/443 blocked)."
}

# Read-only checks of a running install, from this machine.
checks() {
  say "Checks"
  local domain failed=0 code opts=(--noproxy '*' --resolve "$(env_get ROUTER_DOMAIN):443:127.0.0.1"
    --resolve "$(env_get ROUTER_DOMAIN):80:127.0.0.1" --max-time 10 -s)
  domain=$(env_get ROUTER_DOMAIN)
  [[ $(env_get CADDY_GLOBAL_OPTIONS) == *local_certs* ]] && opts+=(--insecure)
  ok() { printf '  \033[32mok\033[0m    %s\n' "$1"; }
  bad() {
    printf '  \033[31mFAIL\033[0m  %s\n' "$1"
    failed=1
  }

  code=$(curl "${opts[@]}" -o /dev/null -w '%{http_code}' "http://$domain/") || true
  if [[ $code == 308 ]]; then ok "http:// sends visitors to https://"; else bad "http:// answered $code, expected a 308 redirect"; fi
  if curl "${opts[@]}" -D - -o /dev/null "https://$domain/" | grep -qi '^strict-transport-security:'; then
    ok "HTTPS, with browsers told to stay on HTTPS (HSTS)"
  else
    bad "no HSTS header on https://$domain/"
  fi
  local path
  for path in /admin /docs /openapi.json; do
    code=$(curl "${opts[@]}" -o /dev/null -w '%{http_code}' "https://$domain$path") || true
    if [[ $code == 401 ]]; then ok "$path needs the dashboard login"; else bad "$path answered $code without a login, expected 401"; fi
  done
  code=$(curl "${opts[@]}" -o /dev/null -w '%{http_code}' "https://$domain/api/v1/admin/attorneys") || true
  if [[ $code == 401 ]]; then ok "the admin API needs the admin key"; else bad "the admin API answered $code without a key, expected 401"; fi
  local service port
  for service in lead-router:8000 router-db:5432 mailpit:8025; do
    port=$(docker compose port "${service%:*}" "${service#*:}" 2>/dev/null || true)
    if [[ -z $port || $port == *":0" ]]; then ok "${service%:*} isn't reachable from outside"; else bad "${service%:*} is published on $port"; fi
  done
  local open
  open=$(curl "${opts[@]}" "https://$domain/api/v1/meta" |
    python3 -c 'import json, sys; print(", ".join(json.load(sys.stdin)["open"]) or "none")' 2>/dev/null) || open="unknown"
  if [[ $open == none ]]; then
    ok "accepting inquiries from: no state yet (soft launch)"
  else
    ok "accepting inquiries from: $open"
  fi
  ((failed == 0)) || fail "some checks failed (above)."
}

summary() {
  local domain
  domain=$(env_get ROUTER_DOMAIN)
  say "Done. It's live."
  note "Inquiry form:   https://$domain"
  note "Dashboard:      https://$domain/admin   (login: $(env_get STAFF_USER), then the admin key below)"
  note "Admin key:      $(env_get ROUTER_ADMIN_API_KEY)"
  note "Privacy policy: https://$domain/privacy"
  note ""
  note "Check it any time:  ./deploy/install.sh status"
  note "Next: docs/DEPLOY.md, from \"After the first install\" (test email, backups off the server)."
}

case ${1:-install} in
  install)
    if [[ ${DEPLOY_SKIP_SYSTEM:-0} != 1 ]]; then system_setup; fi
    command -v docker >/dev/null 2>&1 || fail "Docker isn't installed."
    configure
    check_dns
    start
    verify
    checks
    if [[ ${DEPLOY_SKIP_SYSTEM:-0} != 1 ]]; then backup_schedule; fi
    summary
    ;;
  update)
    [[ -f .env && -n $(env_get STAFF_PASSWORD_HASH) ]] || fail "run sudo ./deploy/install.sh first."
    say "Getting the latest code"
    git pull --ff-only
    start
    verify
    say "Updated."
    ;;
  status)
    docker compose ps
    verify
    checks
    ;;
  *)
    fail "unknown command '$1' (use install, update or status)"
    ;;
esac
