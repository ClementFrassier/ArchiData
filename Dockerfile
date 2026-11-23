# Image du pipeline factures : récupération S3 → bronze → silver.
# Construction : docker build -t factures .
# Lancement    : docker run --rm --env-file .env factures
# Les secrets ne sont PAS dans l'image : ils sont fournis au lancement (--env-file).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /bin/uv

WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never

# 1) Dépendances seules (couche mise en cache tant que pyproject/uv.lock ne changent pas)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Code du projet
COPY src ./src
COPY evaluation ./evaluation
RUN uv sync --frozen --no-dev

# Utilisateur sans privilèges
RUN useradd --create-home factures && chown -R factures /app
USER factures

ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/app/data

ENTRYPOINT ["factures"]
CMD ["pipeline"]
