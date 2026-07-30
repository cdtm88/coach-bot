FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
# Data the installed package looks for on disk rather than importing.
#
# `agent/persona.py` and `seed.py` both resolve a default path relative to the
# source tree, and that does not survive a pip install into site-packages —
# `parents[3]` there lands inside the interpreter's lib directory, not the
# checkout. So both are copied to fixed locations under /app and pointed at
# explicitly: PERSONA_PATH in the compose file, `coach-seed --file` by hand.
# Without the copy, `coach-seed` cannot be run as a one-off container at all.
COPY prompts ./prompts
COPY seeds ./seeds

RUN pip install --no-cache-dir .

# Runs unprivileged. Nothing here needs root.
#
# The uid is pinned rather than left to the base image because the compose stack
# bind-mounts host directories for the FIT archive, the drop folder and the
# backups. A container that cannot write to them fails at the first activity
# rather than at boot, which is the worst time to find out. `chown -R 10001`
# those paths on the host; docs/setup.md says so in the step that creates them.
RUN useradd --create-home --uid 10001 coach && chown -R coach /app
USER coach

CMD ["coach-migrate"]
