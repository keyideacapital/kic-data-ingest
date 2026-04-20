#!/usr/bin/env bash
# kic-ingest-install.sh — one-shot installer for any KIC project.
#
# Usage (from the root of a KIC repo):
#     curl -sSL https://raw.githubusercontent.com/keyideacapital/kic-data-ingest/main/install.sh | bash
# or, if running locally against the source tree:
#     bash kic-ingest-install.sh
#
# What it does, in order:
#   1. Detects whether this is a git repo (warns if not)
#   2. Copies the skill bundle into ./skills/kic-data-ingest/
#   3. Copies CLAUDE.md into the repo root (merges if one already exists)
#   4. Adds .ingest-out/ to .gitignore
#   5. Installs Python deps into a project-local venv or the user site
#   6. Prints the quickstart
#
# Idempotent — rerun any time to update an already-installed repo.

set -euo pipefail

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
BLUE=$'\033[0;34m'
DIM=$'\033[2m'
RESET=$'\033[0m'

log()    { echo "${BLUE}→${RESET} $*"; }
ok()     { echo "${GREEN}✓${RESET} $*"; }
warn()   { echo "${YELLOW}⚠${RESET} $*"; }
die()    { echo "${RED}✗${RESET} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Locate the skill source
# ---------------------------------------------------------------------------

# Source resolution order:
#   1. $KIC_SKILL_SRC env var (explicit override)
#   2. The skill bundle sitting next to this script (when running from a clone)
#   3. Download from the keyideacapital/kic-data-ingest repo (fallback)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_BUNDLE_SRC="${SCRIPT_DIR}/skill/kic-data-ingest"

if [ -n "${KIC_SKILL_SRC:-}" ] && [ -d "$KIC_SKILL_SRC" ]; then
  BUNDLE_SRC="$KIC_SKILL_SRC"
elif [ -d "$DEFAULT_BUNDLE_SRC" ]; then
  BUNDLE_SRC="$DEFAULT_BUNDLE_SRC"
else
  # Remote fetch path — left unimplemented until the canonical repo exists.
  # Emits a clear error rather than silently doing the wrong thing.
  die "Cannot locate kic-data-ingest skill bundle.
      Set KIC_SKILL_SRC to point at the skill directory, or run this script
      from a clone of keyideacapital/kic-data-ingest."
fi

log "Using skill bundle at: ${DIM}${BUNDLE_SRC}${RESET}"

# ---------------------------------------------------------------------------
# Sanity-check the target
# ---------------------------------------------------------------------------

TARGET_DIR="$(pwd)"
log "Installing into: ${DIM}${TARGET_DIR}${RESET}"

if [ ! -d ".git" ]; then
  warn "No .git directory — this doesn't look like a repo. Continuing anyway."
fi

if [ -d "skills/kic-data-ingest" ]; then
  log "Skill already installed; updating in place."
fi

# ---------------------------------------------------------------------------
# 1. Copy the skill bundle
# ---------------------------------------------------------------------------

log "Copying skill bundle → ./skills/kic-data-ingest/"
mkdir -p skills
# Use rsync if available (preserves perms + faster on reruns); fall back to cp.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "$BUNDLE_SRC/" "skills/kic-data-ingest/"
else
  rm -rf skills/kic-data-ingest
  cp -r "$BUNDLE_SRC" skills/kic-data-ingest
fi
ok "Skill bundle installed"

# ---------------------------------------------------------------------------
# 2. Drop / merge CLAUDE.md
# ---------------------------------------------------------------------------

CLAUDE_MD_SRC="${SCRIPT_DIR}/CLAUDE.md"
if [ ! -f "$CLAUDE_MD_SRC" ]; then
  die "CLAUDE.md template not found at $CLAUDE_MD_SRC"
fi

if [ -f "CLAUDE.md" ] && ! grep -q "kic-data-ingest" CLAUDE.md; then
  # Existing CLAUDE.md with unrelated content — append ours under a marker.
  log "Existing CLAUDE.md found; appending ingest section."
  {
    echo ""
    echo "<!-- BEGIN kic-data-ingest section (managed by kic-ingest-install.sh) -->"
    cat "$CLAUDE_MD_SRC"
    echo "<!-- END kic-data-ingest section -->"
  } >> CLAUDE.md
  ok "Appended ingest pattern to existing CLAUDE.md"
elif [ -f "CLAUDE.md" ] && grep -q "kic-data-ingest" CLAUDE.md; then
  # Already has our section — replace it cleanly.
  log "Updating existing kic-data-ingest section in CLAUDE.md"
  python3 - "$CLAUDE_MD_SRC" <<'PYEOF'
import re, sys
template = open(sys.argv[1]).read()
existing = open("CLAUDE.md").read()
pattern = re.compile(
    r"<!-- BEGIN kic-data-ingest section.*?-->.*?<!-- END kic-data-ingest section -->",
    re.DOTALL,
)
if pattern.search(existing):
    new = pattern.sub(
        "<!-- BEGIN kic-data-ingest section (managed by kic-ingest-install.sh) -->\n"
        + template +
        "\n<!-- END kic-data-ingest section -->",
        existing,
    )
else:
    new = existing + "\n" + template
open("CLAUDE.md", "w").write(new)
PYEOF
  ok "Updated CLAUDE.md ingest section"
else
  log "Dropping CLAUDE.md into repo root"
  cp "$CLAUDE_MD_SRC" CLAUDE.md
  ok "CLAUDE.md installed"
fi

# ---------------------------------------------------------------------------
# 3. Update .gitignore
# ---------------------------------------------------------------------------

GITIGNORE_ENTRIES=(
  ".ingest-out/"
  "skills/kic-data-ingest/__pycache__/"
)

if [ -f ".gitignore" ]; then
  for entry in "${GITIGNORE_ENTRIES[@]}"; do
    if ! grep -qxF "$entry" .gitignore; then
      echo "$entry" >> .gitignore
      log "Added ${DIM}$entry${RESET} to .gitignore"
    fi
  done
else
  printf "%s\n" "${GITIGNORE_ENTRIES[@]}" > .gitignore
  ok "Created .gitignore"
fi

# ---------------------------------------------------------------------------
# 4. Python deps
# ---------------------------------------------------------------------------

REQUIRED_PKGS=("rapidfuzz" "openpyxl")

check_pkg() {
  python3 -c "import $1" 2>/dev/null
}

missing=()
for pkg in "${REQUIRED_PKGS[@]}"; do
  if ! check_pkg "$pkg"; then
    missing+=("$pkg")
  fi
done

if [ ${#missing[@]} -eq 0 ]; then
  ok "Python deps already installed: ${REQUIRED_PKGS[*]}"
else
  log "Installing Python deps: ${missing[*]}"
  # Prefer project venv if present, then user install, then --break-system-packages as last resort.
  if [ -n "${VIRTUAL_ENV:-}" ]; then
    pip install "${missing[@]}" --quiet
  elif python3 -m pip install --user "${missing[@]}" --quiet 2>/dev/null; then
    :
  else
    pip install "${missing[@]}" --break-system-packages --quiet
  fi
  ok "Python deps installed"
fi

# ---------------------------------------------------------------------------
# 5. Smoke test: can we import the engine's deps cleanly?
# ---------------------------------------------------------------------------

if python3 -c "
import sys
sys.path.insert(0, 'skills/kic-data-ingest/scripts')
# Just import the module to verify the skill is wired correctly.
import engine
assert hasattr(engine, 'main'), 'engine.main missing'
" 2>/dev/null; then
  ok "Smoke test passed"
else
  warn "Smoke test failed — check that skills/kic-data-ingest/scripts/engine.py is intact"
fi

# ---------------------------------------------------------------------------
# Quickstart
# ---------------------------------------------------------------------------

cat <<'EOF'

──────────────────────────────────────────────────────────────────
  kic-data-ingest installed.

  Next steps:

    1. Set your secrets (in this shell, or via project .env / wrangler):
         export AIRTABLE_API_KEY=pat...
         export AIRTABLE_BASE_ID=app...
         export ANTHROPIC_API_KEY=sk-ant-...   # only if using enrichment

    2. Drop a CSV or XLSX file into the repo and run:
         python skills/kic-data-ingest/scripts/engine.py FILE.csv --out .ingest-out/
         python skills/kic-data-ingest/scripts/write_to_crm.py .ingest-out/ --mode dry-run

    3. If happy, apply:
         python skills/kic-data-ingest/scripts/write_to_crm.py .ingest-out/ --mode apply

  Claude Code sessions in this repo will now pick up the ingest pattern
  automatically via CLAUDE.md. Just ask "ingest this file".

──────────────────────────────────────────────────────────────────
EOF
