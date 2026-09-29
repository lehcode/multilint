# Stage 1: markdownlint and prettier (node:20 includes npm by default)
FROM node:20 AS node-tools
RUN npm install -g markdownlint-cli prettier

FROM python:3.12-slim AS final

LABEL io.modelcontextprotocol.server.name="io.github.lehcode/multilint"
LABEL io.modelcontextprotocol.server.version="0.1.0"
LABEL org.opencontainers.image.description="Code quality delegation for AI agents — shellcheck, pylint, black, gitleaks via one MCP tool"

ENV DEBIAN_FRONTEND=noninteractive

# USER nobody  # moved below: every build step that follows (apt-get, corepack,
# pip, the gitleaks extraction into /usr/local/bin, chmod) requires root, so
# dropping privileges here failed the build with apt exit code 100. The drop now
# happens immediately before ENTRYPOINT, which leaves the runtime unprivileged
# while letting the image build.

# markdownlint-cli is an ESM CLI that resolves commander, micromatch and the
# rest through node_modules at run time.
#
# The previous form was:
#     COPY --from=node-tools /usr/local/bin/markdownlint /usr/local/bin/markdownlint
# In the node-tools stage that path is a symlink into
# /usr/local/lib/node_modules/markdownlint-cli/, and COPY dereferences it, so the
# image received a standalone 12 KB markdownlint.js and no dependency tree at
# all — /usr/local/lib/node_modules did not exist. Every invocation died with
# "Cannot find package 'commander' imported from /usr/local/bin/markdownlint",
# which lint.sh then swallowed and reported as ✓. Verified in the running image.
#
# Copying the tree and recreating the launcher as a symlink keeps node's
# resolution walking up from the real script location, which is what finds the
# hoisted top-level dependencies.
COPY --from=node-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/markdownlint-cli/markdownlint.js \
        /usr/local/bin/markdownlint \
    && chmod +x /usr/local/lib/node_modules/markdownlint-cli/markdownlint.js

RUN apt-get update && apt-get install -y \
    shellcheck \
    shfmt \
    bash \
    curl \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Bootstrap npm via corepack (avoids heavy npm apt package)
RUN corepack npm install -g prettier

RUN pip install --no-cache-dir \
    flake8 \
    pylint \
    black \
    mypy \
    bashate \
    bandit \
    fastmcp \
    mcp

# Install toml-sort (TOML linting), and gitleaks
RUN pip install --no-cache-dir toml-sort
RUN curl -fsSL https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz | \
    tar xz -C /usr/local/bin

WORKDIR /workspace

COPY lint.sh /usr/local/bin/lint.sh
COPY .gitleaks.toml /usr/local/bin/.gitleaks.toml
COPY server.py /usr/local/bin/server.py
COPY mcp_server.py /usr/local/bin/mcp_server.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Make scripts executable
RUN chmod +x /usr/local/bin/server.py /usr/local/bin/mcp_server.py /usr/local/bin/entrypoint.sh

# Run as non-root user (security best practice). Last instruction before the
# entrypoint so every build step above still runs as root.
USER nobody

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
