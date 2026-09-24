# Build from the repository root.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/python ./packages/python
COPY products/enterprise/backend ./products/enterprise/backend
COPY src ./src
RUN uv sync --frozen --package covalent-enterprise --no-dev --no-editable
COPY skills/built_in ./skills/built_in
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 5170
CMD ["covalent-enterprise", "serve", "--host", "0.0.0.0", "--port", "5170"]
