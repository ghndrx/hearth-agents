# Multi-stage Dockerfile for hearth-agents
# Stage 1: Build TypeScript
FROM node:22-alpine AS builder

WORKDIR /app

# Install build dependencies for better-sqlite3
RUN apk add --no-cache python3 make g++ git

COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json ./
COPY src/ ./src/

RUN npx tsc

# Stage 2: Production runtime
FROM node:22-alpine

WORKDIR /app

# Runtime dependencies: git for worktrees, gh for PR creation, openssh for git over ssh
RUN apk add --no-cache git openssh-client curl ca-certificates \
    && wget -q https://github.com/cli/cli/releases/download/v2.60.1/gh_2.60.1_linux_amd64.tar.gz \
    && tar -xzf gh_2.60.1_linux_amd64.tar.gz \
    && mv gh_2.60.1_linux_amd64/bin/gh /usr/local/bin/ \
    && rm -rf gh_2.60.1_linux_amd64* \
    && apk del --purge

# Install production dependencies only
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

# Copy compiled JS
COPY --from=builder /app/dist ./dist

# Create non-root user
RUN addgroup -S agent && adduser -S agent -G agent \
    && mkdir -p /data /app/logs \
    && chown -R agent:agent /data /app/logs

USER agent

# Expose metrics and webhook ports
EXPOSE 9090 9091

# Data volume for SQLite DB and logs
VOLUME ["/data", "/app/logs"]

# Health check hits the metrics endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD wget -qO- http://localhost:9090/metrics > /dev/null || exit 1

CMD ["node", "dist/main.js"]
