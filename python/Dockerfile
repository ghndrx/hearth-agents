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

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
