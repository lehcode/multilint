#!/usr/bin/env bash
#
# scripts/sync-core.sh — Sync core/ files to target plugin directories.
#
# core/ is the single source of truth for the shared engine. A GitHub-installed
# Claude Code plugin receives committed copies of these scripts inside its
# package tree — no build step runs, symlinks don't survive the plugin cache,
# and the plugin is installed from a git tag.
#
# Workflow:
#   1. Edit files in core/
#   2. Run scripts/sync-core.sh  (to overwrite the committed copies)
#   3. Commit core/ and claude-plugin/scripts/ together
#
# Usage:
#   scripts/sync-core.sh            # copy core → targets (default)
#   scripts/sync-core.sh --check   # fail if drift detected, no writes
#

set -euo pipefail

# Resolve the repository root (works from any subdirectory)
REPO_ROOT="$(cd "$(git rev-parse --show-toplevel)" && pwd)"

# Target directories that receive committed copies of core/ files.
# Every plugin ships its own copy, because an installed plugin runs no build step.
TARGETS=(
    "${REPO_ROOT}/claude-plugin/scripts"
    "${REPO_ROOT}/opencode-plugin/scripts"
)

# Collect every regular file directly under core/ (no subdirectories).
CORE_FILES=()
while IFS= read -r -d '' file; do
    CORE_FILES+=("${file}")
done < <(find "${REPO_ROOT}/core" -maxdepth 1 -type f -print0 | sort -z)

check_mode=0
for arg in "$@"; do
    if [ "${arg}" = "--check" ]; then
        check_mode=1
    fi
done

# --- check mode: detect drift without writing ---
if [ "${check_mode}" -eq 1 ]; then
    drift_found=0

    for target in "${TARGETS[@]}"; do
        # A missing target is drift, not a pass: the plugin would ship without its engine.
        if [ ! -d "${target}" ]; then
            echo "drift: ${target} (missing)" >&2
            drift_found=1
            continue
        fi

        # Compare each core file and its mode
        for core_file in "${CORE_FILES[@]}"; do
            file="$(basename "${core_file}")"
            target_file="${target}/${file}"

            if ! cmp -s "${core_file}" "${target_file}"; then
                echo "drift: ${target_file}" >&2
                drift_found=1
                continue
            fi

            # Check file mode
            core_mode="$(stat -c '%a' "${core_file}")"
            target_mode="$(stat -c '%a' "${target_file}")"

            if [ "${core_mode}" != "${target_mode}" ]; then
                echo "drift: ${target_file} " \
                    "(mode ${target_mode}, expected ${core_mode})" >&2
                drift_found=1
            fi
        done

        # Detect extra files in the target that are not in core/
        while IFS= read -r -d '' target_file; do
            file="$(basename "${target_file}")"
            found=0
            for core_file in "${CORE_FILES[@]}"; do
                if [ "$(basename "${core_file}")" = "${file}" ]; then
                    found=1
                    break
                fi
            done
            if [ "${found}" -eq 0 ]; then
                echo "drift: ${target_file} (extra)" >&2
                drift_found=1
            fi
        done < <(find "${target}" -maxdepth 1 -type f -print0)
    done

    if [ "${drift_found}" -ne 0 ]; then
        exit 1
    fi

    exit 0
fi

# --- default mode: copy core → targets ---
for target in "${TARGETS[@]}"; do
    [ -d "${target}" ] || mkdir -p "${target}"

    # Copy each core file, preserving attributes (exec bits)
    for core_file in "${CORE_FILES[@]}"; do
        file="$(basename "${core_file}")"
        target_file="${target}/${file}"
        cp -p "${core_file}" "${target_file}"
    done

    # Remove files in target that no longer exist in core/
    while IFS= read -r -d '' target_file; do
        file="$(basename "${target_file}")"
        found=0
        for core_file in "${CORE_FILES[@]}"; do
            if [ "$(basename "${core_file}")" = "${file}" ]; then
                found=1
                break
            fi
        done
        if [ "${found}" -eq 0 ]; then
            rm -f "${target_file}"
        fi
    done < <(find "${target}" -maxdepth 1 -type f -print0)
done
