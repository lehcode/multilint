# Stage 1: markdownlint and prettier (node:20 includes npm by default)
FROM node:20 AS node-tools
RUN npm install -g markdownlint-cli prettier

FROM python:3.12-slim AS final

LABEL io.modelcontextprotocol.server.name="io.github.username/multilint"
LABEL io.modelcontextprotocol.server.version="0.1.0"
LABEL org.opencontainers.image.description="Code quality delegation for AI agents — shellcheck, pylint, black, gitleaks via one MCP tool"

ENV DEBIAN_FRONTEND=noninteractive

COPY --from=node-tools /usr/local/bin/markdownlint /usr/local/bin/markdownlint

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

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
