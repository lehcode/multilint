# Linter toolchain for the multilint editor plugins.
#
# The image carries the linters only. Each plugin mounts its own lint.sh read-only at /opt/multilint
# and runs it in an ephemeral container (docker run --rm --network none --user uid:gid), so there is
# no entrypoint, no server and no copy of lint.sh in here.
#
# Every tool is pinned. Tools without a Python or npm package are downloaded per TARGETARCH (amd64,
# arm64) and checked against a SHA-256. The gitleaks and shfmt sums match the upstream checksum
# files; shellcheck publishes none, so its sums were taken from the release assets. Changing a
# version means changing its sums, or the build fails.

ARG NODE_IMAGE=node:24.22.0-trixie-slim
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie

# --- markdownlint and prettier -------------------------------------------------------------------
FROM ${NODE_IMAGE} AS node-tools
RUN npm install --global --no-fund --no-audit --loglevel=error markdownlint-cli@0.48.0 prettier@3.9.10

# --- shellcheck, shfmt and gitleaks --------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS binaries
ARG TARGETARCH
ARG SHELLCHECK_VERSION=0.10.0
ARG SHFMT_VERSION=3.8.0
ARG GITLEAKS_VERSION=8.30.1
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends ca-certificates curl xz-utils >/dev/null
WORKDIR /download
RUN set -eu; \
    case "$TARGETARCH" in \
        amd64) \
            sc_arch=x86_64; gl_arch=x64; \
            sc_sum=6c881ab0698e4e6ea235245f22832860544f17ba386442fe7e9d629f8cbedf87; \
            sf_sum=27b3c6f9d9592fc5b4856c341d1ff2c88856709b9e76469313642a1d7b558fe0; \
            gl_sum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;; \
        arm64) \
            sc_arch=aarch64; gl_arch=arm64; \
            sc_sum=324a7e89de8fa2aed0d0c28f3dab59cf84c6d74264022c00c22af665ed1a09bb; \
            sf_sum=27e1f69b0d57c584bcbf5c882b4c4f78ffcf945d0efef45c1fbfc6692213c7c3; \
            gl_sum=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    gh=https://github.com; \
    curl -fsSL -o shellcheck.tar.xz \
        "$gh/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.${sc_arch}.tar.xz"; \
    curl -fsSL -o shfmt \
        "$gh/mvdan/sh/releases/download/v${SHFMT_VERSION}/shfmt_v${SHFMT_VERSION}_linux_${TARGETARCH}"; \
    curl -fsSL -o gitleaks.tar.gz \
        "$gh/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${gl_arch}.tar.gz"; \
    printf '%s  %s\n' "$sc_sum" shellcheck.tar.xz "$sf_sum" shfmt "$gl_sum" gitleaks.tar.gz | sha256sum -c --quiet -; \
    mkdir bin; \
    tar -xJf shellcheck.tar.xz --strip-components=1 -C bin "shellcheck-v${SHELLCHECK_VERSION}/shellcheck"; \
    install -m 0755 shfmt bin/shfmt; \
    tar -xzf gitleaks.tar.gz -C bin gitleaks

# --- toolchain -----------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="multilint" \
      org.opencontainers.image.description="Linter toolchain for the multilint editor plugins" \
      org.opencontainers.image.source="https://github.com/lehcode/multilint"

RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
    bandit==1.9.4 \
    bashate==2.1.1 \
    black==26.10.1 \
    flake8==7.4.1 \
    mypy==2.4.0 \
    pylint==4.1.3 \
    toml-sort==0.25.0

# gitleaks scans history (`detect --source <repo>`) whenever the target sits inside a repository, and
# that mode shells out to git. Without it every such run died with `"git": executable file not found`
# and lint.sh reported gitleaks as skipped. The version follows the pinned base image's Debian release.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends git >/dev/null \
    && rm -rf /var/lib/apt/lists/*

# One Node: the binary and the two global packages, each with its own node_modules tree. Copying
# only the launcher (an earlier form of this file) left markdownlint without its dependencies, and
# every run died with "Cannot find package 'commander'".
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules/markdownlint-cli /usr/local/lib/node_modules/markdownlint-cli
COPY --from=node-tools /usr/local/lib/node_modules/prettier /usr/local/lib/node_modules/prettier
RUN ln -s ../lib/node_modules/markdownlint-cli/markdownlint.js /usr/local/bin/markdownlint \
    && ln -s ../lib/node_modules/prettier/bin/prettier.cjs /usr/local/bin/prettier

COPY --from=binaries /download/bin/ /usr/local/bin/
# lint.sh looks for the gitleaks config here before the copy beside itself.
COPY .gitleaks.toml /usr/local/bin/.gitleaks.toml

# Fail the build, not the first lint, when a tool cannot start.
RUN set -e; \
    for tool in shellcheck shfmt bashate flake8 black pylint mypy bandit markdownlint prettier toml-sort git; do \
        "$tool" --version >/dev/null; \
    done; \
    gitleaks version >/dev/null

USER nobody
