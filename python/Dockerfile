FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev

COPY hearth_agents ./hearth_agents
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev


FROM python:3.12-slim

# Toolchains the agent needs to build+test the target repos:
#   - Go 1.25 for hearth/backend (go.mod says go 1.25.8)
#   - Node 20 LTS + npm for hearth-desktop and any TS tests
#   - git/ca-certs for worktree ops
#   - curl/xz/build-essential for fetching and installing Go + node-gyp deps
# Without these, every attempted feature fails on `go build` exit=127,
# which is why 100% of attempts were timing out before this was added.
ARG GO_VERSION=1.25.8
ARG NODE_MAJOR=20
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl xz-utils build-essential \
    && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-$(dpkg --print-architecture).tar.gz" \
        | tar -C /usr/local -xz \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}" GOPATH=/root/go

# The container runs as root but the bind-mounted /repos are owned by the host
# user (uid 1000). Without this, git refuses every worktree op with "dubious
# ownership" and every product feature fails at worktree_add.
RUN git config --global --add safe.directory '*' \
    && git config --global user.email "agent@hearth-agents.local" \
    && git config --global user.name "Hearth Agents"

WORKDIR /app
COPY --from=builder /app /app

# Stamp build info so /build endpoint can report expected-vs-running.
# Accepts --build-arg GIT_SHA=... GIT_BRANCH=... from the host; the
# ``scripts/hearth deploy`` CLI injects them automatically.
ARG GIT_SHA=unknown
ARG GIT_BRANCH=unknown
RUN echo "git_sha=${GIT_SHA}" > /app/BUILD_INFO \
    && echo "git_branch=${GIT_BRANCH}" >> /app/BUILD_INFO \
    && echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> /app/BUILD_INFO

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
VOLUME ["/data", "/repos"]
EXPOSE 8000

CMD ["python", "-m", "hearth_agents.main"]
