# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy 

WORKDIR /app

# Copy dependency files required by the package metadata
COPY pyproject.toml ./

# Install dependencies
RUN uv sync --no-dev

# Copy the rest of the application
COPY . .

# Expose the API port
EXPOSE 5170

# Set environment variables
ENV HOST=0.0.0.0
ENV PORT=5170
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["uv", "run", "uvicorn", "agent_framework.api.app:create_app", "--host", "0.0.0.0", "--port", "5170", "--factory"]
