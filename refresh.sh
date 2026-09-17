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
# Exit codes: 0 ok, 1 scrape/build failed, 2 verify gate failed (nothing shipped),
#             3 source files are dirty (commit or stash them first)
# ---------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

PY="${PY:-C:/Users/Administrator/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe}"
WRANGLER="${WRANGLER:-C:/Users/Administrator/.workbuddy-ai/binaries/node/workspace/wranglerproj/node_modules/.bin/wrangler.cmd}"
CF_DIR="${CF_DIR:-C:/Users/Administrator/.workbuddy-ai/cloudflare}"
PROJECT="${CF_PROJECT:-vps-deals-radar}"
ACCOUNT="${CF_ACCOUNT:-fc6b63f8415dc700028227a3ba6dd399}"
SKIP_DEPLOY="${SKIP_DEPLOY:-}"

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
  MSG="$("$PY" -c 'import json;d=json.load(open("data/offers.json",encoding="utf-8"));print("%d/%d providers priced" % (d["providers_with_price"], d["providers_configured"]))')"
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
