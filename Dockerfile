FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# uv
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies first (cache layer) — include dev for tests
COPY pyproject.toml README.md ./
RUN uv sync --no-install-project --extra dev

# Application code
COPY . .
RUN uv sync --extra dev

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uv", "run", "studioos", "serve"]
