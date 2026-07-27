FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --no-cache-dir .

# Runs unprivileged. Nothing here needs root.
RUN useradd --create-home --uid 10001 coach && chown -R coach /app
USER coach

CMD ["coach-migrate"]
