# Build from the repository root.
FROM node:22-slim AS builder
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY products/enterprise/web ./products/enterprise/web
COPY products/desktop/web/package.json ./products/desktop/web/package.json
COPY products/desktop/shell/package.json ./products/desktop/shell/package.json
RUN pnpm install --frozen-lockfile --ignore-scripts
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build:enterprise

FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3100
RUN groupadd --system --gid 1001 appgroup && useradd --system --uid 1001 --gid appgroup appuser
COPY --from=builder --chown=appuser:appgroup /app/products/enterprise/web/.next/standalone ./
COPY --from=builder --chown=appuser:appgroup /app/products/enterprise/web/public ./products/enterprise/web/public
COPY --from=builder --chown=appuser:appgroup /app/products/enterprise/web/.next/static ./products/enterprise/web/.next/static
USER appuser
EXPOSE 3100
CMD ["node", "products/enterprise/web/server.js"]
