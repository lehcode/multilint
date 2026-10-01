# OpenCode V1 (opencode-ai 1.18.34) for the compatibility harness. Manual tool, never built by CI.
FROM docker:cli AS dockercli

FROM node:22
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
RUN npm install -g opencode-ai@1.18.34
