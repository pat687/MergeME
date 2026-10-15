#!/usr/bin/env bash
# Create .env from .env.example, replacing every __RANDOM__ with a strong random value.
# Never overwrites an existing .env, so it's safe to run twice.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists; leaving it alone."
  exit 0
fi

random() { openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

while IFS= read -r line || [[ -n "$line" ]]; do
  while [[ "$line" == *__RANDOM__* ]]; do
    line="${line/__RANDOM__/$(random)}"
  done
  printf '%s\n' "$line"
done < .env.example > .env

chmod 600 .env
echo "Created .env with fresh random secrets (readable only by you)."
echo "Your admin key for http://localhost:8000/admin is:"
grep '^ROUTER_ADMIN_API_KEY=' .env | cut -d= -f2
