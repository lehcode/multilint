#!/usr/bin/env bash
# Compatibility harness: does the multilint OpenCode plugin load on V1 and V2
# and does the model receive the <multilint> notice? Manual tool, not run by
# pytest or CI. See --help.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
src="$repo/scripts/opencode-compat"
v1_image="multilint-opencode-compat:1.18.34"
multilint_image="lehcode/multilint:latest"

usage() {
    cat <<'USAGE'
Usage: scripts/opencode-compat/run.sh [options]

  --version v1|v2|all     OpenCode generation (default all). V2 runs on the
                          host (OPENCODE_V2_BIN, default opencode), V1 in the
                          multilint-opencode-compat:1.18.34 container
  --tier shim|docker      shim: fake docker records argv (default); docker:
                          real docker, needs lehcode/multilint:latest
  --source local|tarball|all
                          local: plugin files from this checkout; tarball:
                          npm pack installed as a file:// plugin (default all)
  --plugin-entry          add the "plugin" entry to the copied opencode.json
  --agent-prompt          also run with --agent multilint and assert the
                          agent prompt reaches the model
  --keep                  keep the work dir
  --help                  this text

Exit 0 only when every selected scenario passes. Env: MOCK_PORT (18765).
The Docker image is built on first use of V1.
USAGE
}

version=all tier=shim source=all plugin_entry=0 agent_prompt=0 keep=0
while (($#)); do
    case "$1" in
    --version)
        version="${2:?}"
        shift
        ;;
    --tier)
        tier="${2:?}"
        shift
        ;;
    --source)
        source="${2:?}"
        shift
        ;;
    --plugin-entry) plugin_entry=1 ;;
    --agent-prompt) agent_prompt=1 ;;
    --keep) keep=1 ;;
    --help | -h)
        usage
        exit 0
        ;;
    *)
        echo "unknown option: $1" >&2
        usage >&2
        exit 2
        ;;
    esac
    shift
done

work="$(mktemp -d "${TMPDIR:-/tmp}/multilint-oc-compat.XXXXXX")"
trap 'if ((keep)); then echo "work dir kept: $work"; else chmod -R u+w "$work"; rm -rf "$work"; fi' EXIT

if [[ "$tier" == docker ]]; then
    docker image inspect "$multilint_image" >/dev/null 2>&1 || {
        echo "missing $multilint_image: docker pull $multilint_image" >&2
        exit 2
    }
fi

cp "$src/scenario.sh" "$src/mock-provider.mjs" "$work/"
mkdir -p "$work/bin"
cp "$src/bin/docker" "$work/bin/docker"

prepare_local() { # $1 project dir
    (cd "$repo" && git ls-files .opencode src agents opencode.json |
        while IFS= read -r f; do
            [[ -f "$f" ]] && install -D "$f" "$1/$f"
        done)
    git -C "$1" init -q
}

prepare_tarball() { # $1 project dir
    if [[ ! -d "$work/pkg/node_modules" ]]; then
        (cd "$repo" && npm run build >"$work/build.log" 2>&1 &&
            npm pack --pack-destination "$work" >>"$work/build.log" 2>&1 &&
            npm install --prefix "$work/pkg" "$work"/*.tgz \
                >>"$work/build.log" 2>&1) || {
            tail -20 "$work/build.log" >&2
            return 1
        }
    fi
    mkdir -p "$1"
    cat >"$1/opencode.json" <<JSON
{"plugin": ["file://$work/pkg/node_modules/@lehcode/multilint/dist/dual.js"]}
JSON
    git -C "$1" init -q
}

write_config() { # $1 xdg config home, $2 port
    mkdir -p "$1/opencode"
    cat >"$1/opencode/opencode.json" <<JSON
{
  "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "name": "Mock",
    "options": {"baseURL": "http://127.0.0.1:$2/v1", "apiKey": "mock"},
    "models": {"mock-model": {"name": "Mock", "tool_call": true}}}},
  "model": "mock/mock-model",
  "small_model": "mock/mock-model",
  "permission": {"edit": "allow", "bash": "allow"},
  "autoupdate": false,
  "share": "disabled"
}
JSON
}

run_one() { # $1 gen, $2 source, $3 agent
    local gen="$1" srcname="$2" agent="$3" port="${MOCK_PORT:-18765}"
    local dir="$work/$gen-$srcname-$agent"
    local project="$dir/project" cache check
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/multilint-opencode-compat/$gen"
    mkdir -p "$dir/config" "$dir/data" "$dir/state" "$dir/home" "$cache"
    if [[ "$srcname" == local ]]; then
        prepare_local "$project" || return 1
    else
        prepare_tarball "$project" || return 1
    fi
    if ((plugin_entry)) && [[ "$srcname" == local ]]; then
        node -e 'const f=process.argv[1],fs=require("fs");
            const c=JSON.parse(fs.readFileSync(f,"utf8"));
            c.plugin=[process.argv[2]];
            fs.writeFileSync(f,JSON.stringify(c))' \
            "$project/opencode.json" "${PLUGIN_ENTRY:-./.opencode/plugins/multilint.js}"
    fi
    write_config "$dir/config" "$port"
    check="--docker-log $dir/docker.log"
    [[ "$tier" == docker ]] && check=""
    local expect=""
    [[ "$agent" == multilint ]] && expect="You are the multilint agent."

    local envs=(
        RUN_DIR="$dir" MOCK_PROJECT="$project" MOCK_PORT="$port"
        AGENT="$agent" OC_GEN="$gen" CHECK_ARGS="$check" EXPECT_SYSTEM="$expect"
        FAKE_DOCKER_LOG="$dir/docker.log"
        XDG_CONFIG_HOME="$dir/config" XDG_DATA_HOME="$dir/data"
        XDG_STATE_HOME="$dir/state" XDG_CACHE_HOME="$cache"
        OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_MODELS_FETCH=1
    )
    local status=0
    if [[ "$gen" == v2 ]]; then
        local path="$PATH"
        [[ "$tier" == shim ]] && path="$work/bin:$PATH"
        env "${envs[@]}" PATH="$path" OC_BIN="${OPENCODE_V2_BIN:-opencode}" \
            bash "$work/scenario.sh" || status=1
    else
        docker image inspect "$v1_image" >/dev/null 2>&1 ||
            docker build -t "$v1_image" "$src" >/dev/null || return 1
        local d=(docker run --rm --init --user "$(id -u):$(id -g)"
        --mount "type=bind,source=$work,target=$work"
        --mount "type=bind,source=$cache,target=$cache"
        --workdir "$project" -e "HOME=$dir/home")
        if [[ "$tier" == docker ]]; then
            d+=(--mount "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock"
                --group-add "$(stat -c %g /var/run/docker.sock)")
        else
            envs+=("PATH=$work/bin:/usr/local/bin:/usr/bin:/bin")
        fi
        local e
        for e in "${envs[@]}"; do
            d+=(-e "$e")
        done
        "${d[@]}" -e OC_BIN=opencode "$v1_image" bash "$work/scenario.sh" ||
            status=1
    fi
    if ((status)); then
        echo "logs: $dir (opencode.log, requests.jsonl, docker.log)" >&2
        # shellcheck disable=SC2034 # read by the EXIT trap
        keep=1
        tail -15 "$dir/opencode.log" >&2 2>/dev/null
    fi
    return "$status"
}

gens=(v1 v2) srcs=(local tarball)
[[ "$version" != all ]] && gens=("$version")
[[ "$source" != all ]] && srcs=("$source")
rc=0
for gen in "${gens[@]}"; do
    for s in "${srcs[@]}"; do
        agents=(build)
        if ((agent_prompt)); then agents=(multilint); fi
        for a in "${agents[@]}"; do
            echo "== $gen $s tier=$tier agent=$a"
            if run_one "$gen" "$s" "$a"; then
                echo "RESULT $gen $s $a: PASS"
            else
                echo "RESULT $gen $s $a: FAIL"
                rc=1
            fi
        done
    done
done
exit "$rc"
