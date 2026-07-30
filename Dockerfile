# Two stages. The build stage holds uv and the wheel-building toolchain; the
# runtime stage gets the resolved virtualenv and nothing else, so uv is not
# shipped and neither is a compiler.

FROM python:3.11-slim AS build

# Pinned, not `latest`. A floating tag here would undo the whole point of the
# frozen lock below: the resolver itself is part of what makes a build
# reproducible, and this is the version that produced `uv.lock`.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies before source, so editing a module does not re-resolve and
# re-download 37 packages. `--no-install-project` installs everything the project
# needs and not the project, which is what makes this layer cacheable.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# --- runtime -----------------------------------------------------------------

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=build /app/.venv /app/.venv

COPY migrations ./migrations
# Data the installed package looks for on disk rather than importing.
#
# `agent/persona.py` and `seed.py` both resolve a default path relative to the
# source tree, and that does not survive an install into site-packages —
# `parents[3]` there lands inside the interpreter's lib directory, not the
# checkout. So both are copied to fixed locations under /app and pointed at
# explicitly: PERSONA_PATH in the compose file, `coach-seed --file` by hand.
# Without the copy, `coach-seed` cannot be run as a one-off container at all.
COPY prompts ./prompts
COPY seeds ./seeds

# Runs unprivileged. Nothing here needs root.
#
# The uid is pinned rather than left to the base image because the compose stack
# bind-mounts host directories for the FIT archive, the drop folder and the
# backups. A container that cannot write to them fails at the first activity
# rather than at boot, which is the worst time to find out. `chown -R 10001`
# those paths on the host; docs/deploy.md makes it a step.
RUN useradd --create-home --uid 10001 coach && chown -R coach /app
USER coach

# Every service overrides this. `coach-migrate` is the default because it is the
# one that must run before any other and the only one that is safe to run twice.
CMD ["coach-migrate"]
