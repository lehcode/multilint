#!/usr/bin/env bash
#
# pre-commit-lint.sh — Git pre-commit hook with gitleaks + multilint.
#
# Steps:
#   1. Run gitleaks on the whole repo (--source ., pass_filenames: false)
#   2. Run multilint via docker exec on affected directories
#
# Install:
#   cp scripts/pre-commit-lint.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
#
# Prerequisites:
#   - gitleaks installed on host (/usr/local/bin/gitleaks)
#   - multilint Docker container running
#
# Uninstall:
#   rm .git/hooks/pre-commit

set -euo pipefail

# ─── Debug ───────────────────────────────────────────────────────────────────
echo "DEBUG: Hook executed" >&2

# ─── Paths ───────────────────────────────────────────────────────────────────
GL_CONFIG=".gitleaks.toml"
GL_BIN="gitleaks"
ML_CONTAINER="multilint"
ML_SCRIPT="/usr/local/bin/lint.sh"

# ─── Helpers ─────────────────────────────────────────────────────────────────
info()  { echo "→ $*"; }
pass()  { echo "  ✓ $*"; }
fail()  { echo "  ✗ $*"; EXIT_CODE=1; }

# ─── Pre-flight checks ──────────────────────────────────────────────────────
if ! command -v "$GL_BIN" >/dev/null 2>&1; then
  echo "✗ [pre-commit] gitleaks not found"
  echo "  Install: brew install gitleaks  or  curl -L https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz | tar xz -C /usr/local/bin"
  exit 1
fi

if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${ML_CONTAINER}$"; then
  echo "✗ [pre-commit] ${ML_CONTAINER} container is not running"
  echo "  Start it with: cd ~/docker-compose.d/multilint && docker compose up -d"
  exit 1
fi

# ─── Phase 1: Gitleaks ──────────────────────────────────────────────────────
info "Running gitleaks (secret detection in git history)..."

gl_output="$(gitleaks detect --source . --config "$GL_CONFIG" --verbose --no-color --no-banner 2>&1)" || true
gl_rc=$?

# Count findings
gl_findings=$(echo "$gl_output" | grep -c "Finding:" 2>/dev/null || echo "0")
gl_findings=${gl_findings:-0}

if [ "$gl_rc" -eq 0 ] || [ "$gl_findings" -eq 0 ]; then
  pass "gitleaks: no leaks found"
else
  echo "$gl_output" | head -50
  fail "gitleaks: $gl_findings findings detected"
fi

# ─── Phase 2: Multilint (affected directories) ──────────────────────────────

# Collect lintable extensions (matching lint.sh patterns)
is_lintable() {
  local ext="${1##*.}"
  case "$ext" in
    sh|bash|py|md|yaml|yml|json|toml) return 0 ;;
    *) return 1 ;;
  esac
}

# Collect directories to lint from staged files
declare -A DIRS_TO_LINT
FILES_MODIFIED=0

while IFS= read -r filepath; do
  [ -z "$filepath" ] && continue
  [ -f "$filepath" ] || continue
  if is_lintable "$filepath"; then
    dir="$(dirname "$filepath")"
    DIRS_TO_LINT["$dir"]=1
    FILES_MODIFIED=$((FILES_MODIFIED + 1))
  fi
done < <(git diff --cached --name-only)

if [ "$FILES_MODIFIED" -eq 0 ]; then
  echo ""
  echo "================================"
  echo "  No lintable files staged"
  echo "================================"
  exit 0
fi

echo ""
info "Linting $FILES_MODIFIED file(s) across ${#DIRS_TO_LINT[@]} directory/directories..."

EXIT_CODE=0
for dir in "${!DIRS_TO_LINT[@]}"; do
  echo "→ Linting directory: $dir"

  output="$(docker exec "$ML_CONTAINER" bash "$ML_SCRIPT" "$dir" 2>&1)" || true
  code=$?

  echo "$output"

  if [ "$code" -ne 0 ]; then
    fail "lint failed in $dir (exit code: $code)"
  else
    pass "All checks passed in $dir"
  fi
done

echo ""
echo "================================"
if [ "$EXIT_CODE" -eq 0 ]; then
  echo "  All checks passed ✓"
else
  echo "  Some checks failed ✗"
fi
echo "================================"

exit $EXIT_CODE
