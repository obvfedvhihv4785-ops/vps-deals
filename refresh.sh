#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-command refresh: scrape -> build -> verify -> commit -> publish.
#
# This exists so the pipeline can be driven by any scheduler without repeating
# the ordering or the safety rules. The order matters and the gate is not
# optional: verify.py runs BEFORE anything is committed or deployed, so a broken
# or badly-degraded crawl never reaches the live site.
#
#   ./refresh.sh              normal run
#   PY=... WRANGLER=... ./refresh.sh   override tool paths
#   SKIP_DEPLOY=1 ./refresh.sh         build and commit only
#
# Machine-specific paths and the Cloudflare account id are NOT in this file.
# They live in $HOME/.workbuddy-ai/vps-deals.env, which is sourced if present;
# see vps-deals.env.example. That keeps local paths and account identifiers out
# of a public repo and lets the script run unchanged on another machine.
#
# Exit codes: 0 ok, 1 scrape/build failed, 2 verify gate failed (nothing shipped),
#             3 source files are dirty (commit or stash them first)
# ---------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

# Machine-local settings, if present. Kept outside the repo so the published
# script depends on nothing specific to one machine: no absolute interpreter
# paths, no account identifiers. See vps-deals.env.example.
LOCAL_ENV="${VPS_DEALS_ENV:-$HOME/.workbuddy-ai/vps-deals.env}"
if [ -f "$LOCAL_ENV" ]; then
  # shellcheck disable=SC1090
  . "$LOCAL_ENV"
fi

PY="${PY:-python3}"
WRANGLER="${WRANGLER:-npx wrangler}"
CF_DIR="${CF_DIR:-$HOME/.workbuddy-ai/cloudflare}"
PROJECT="${CF_PROJECT:-vps-deals-radar}"
ACCOUNT="${CF_ACCOUNT:-}"
SKIP_DEPLOY="${SKIP_DEPLOY:-}"

if [ -z "$ACCOUNT" ]; then
  printf '%s  %s\n' "refresh" "FAIL no Cloudflare account id; set CF_ACCOUNT (see vps-deals.env.example)"
  exit 1
fi

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "refresh start (project=$PROJECT)"

# site/ is a build artefact that has to be reproducible from the committed
# sources — that is what lets anyone clone this repo and rebuild it
# byte-for-byte. build.py runs from the working copy, so if the sources are
# dirty this run would commit output built from code the repository does not
# contain, and the two would silently drift apart. Refuse instead.
SOURCE_PATHS="build.py scraper.py verify.py ilang.py templates .ilang .github"
if [ -n "$(git status --porcelain -- $SOURCE_PATHS 2>/dev/null)" ]; then
  log "FAIL source files are modified; commit or stash them, then re-run:"
  git status --short -- $SOURCE_PATHS
  exit 3
fi

log "-- scrape --"
if ! "$PY" scraper.py; then
  log "FAIL scraper.py exited non-zero; nothing committed or deployed"
  exit 1
fi

log "-- build --"
if ! "$PY" build.py; then
  log "FAIL build.py exited non-zero; nothing committed or deployed"
  exit 1
fi

# The gate. A malformed page, a price that does not match its own quoted
# evidence, a broken history file, or a crawl that lost most of its providers
# all fail here — and then the previous good deployment stays live.
log "-- verify (gate) --"
if ! "$PY" verify.py; then
  log "FAIL verify.py rejected this run; not committing, not deploying"
  exit 2
fi

log "-- commit --"
git add -A data site
if git diff --cached --quiet; then
  log "no changes to commit"
else
  MSG="$("$PY" -c 'import json;d=json.load(open("data/offers.json",encoding="utf-8"));print("%d/%d providers priced (%d read fresh this run)" % (d["providers_with_price_shown"], d["providers_configured"], d["providers_with_price"]))')"
  git -c user.name=promo-radar-bot \
      -c user.email=promo-radar-bot@users.noreply.github.com \
      commit -q -m "data: refresh ${MSG} at $(date -u +%Y-%m-%dT%H:%MZ)"
  log "committed: ${MSG}"
fi

if [ -n "$SKIP_DEPLOY" ]; then
  log "SKIP_DEPLOY set; stopping before publish"
  exit 0
fi

log "-- deploy --"
if [ ! -f "$CF_DIR/api_token.txt" ]; then
  log "no Cloudflare token at $CF_DIR; built and committed but not published"
  exit 0
fi
CLOUDFLARE_API_TOKEN="$(tr -d '\r\n' < "$CF_DIR/api_token.txt")"
export CLOUDFLARE_API_TOKEN
export CLOUDFLARE_ACCOUNT_ID="$ACCOUNT"
if ! "$WRANGLER" pages deploy site --project-name="$PROJECT" --branch=main --commit-dirty=true; then
  log "FAIL deploy step; the previous deployment is still live"
  exit 1
fi

log "refresh done"
