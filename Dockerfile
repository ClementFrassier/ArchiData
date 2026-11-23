# Construction : docker build -t factures .
# Lancement    : docker run --rm --env-file .env factures
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /bin/uv

WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY evaluation ./evaluation
RUN uv sync --frozen --no-dev

RUN useradd --create-home factures && chown -R factures /app
USER factures

ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/app/data

ENTRYPOINT ["factures"]
CMD ["pipeline"]
