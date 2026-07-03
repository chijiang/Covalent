FROM node:22-slim AS base

# Install pnpm
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable

WORKDIR /app

# Install dependencies
FROM base AS deps
COPY package.json pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile --ignore-scripts

# Build the application
FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Disable Next.js telemetry during build
ENV NEXT_TELEMETRY_DISABLED 1
# Set BACKEND_URL at build time for rewrites
ARG BACKEND_URL=http://agent-framework-backend:5170
ENV BACKEND_URL=$BACKEND_URL
# Set CI environment variable for pnpm
ENV CI=true
# Directly use Next.js build to avoid pnpm script issues
RUN npx next build

# Ensure all required directories exist for the COPY command
RUN mkdir -p /app/public /app/.next/static || true

# Final runner stage
FROM node:22-slim AS runner
WORKDIR /app

ENV NODE_ENV production
ENV NEXT_TELEMETRY_DISABLED 1

# Create non-root user
RUN addgroup --system --gid 1001 appgroup && adduser --system --uid 1001 --ingroup appgroup appuser

# Copy standalone output (required)
COPY --from=builder /app/.next/standalone ./

# Copy optional directories (they now exist in builder stage)
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/static/. ./.next/static/

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 3100
ENV HOSTNAME="0.0.0.0"
ENV PORT=3100

CMD ["node", "server.js"]
