# For running the bot somewhere always-on: a small VM, Fly.io, Koyeb, anywhere that
# takes a container. The scheduled workflow in .github/workflows/bot.yml is the free
# alternative and needs no image at all; this exists for when a five-minute reply
# latency is not good enough.
FROM python:3.11-slim

WORKDIR /app

# Dependencies first, from the lockfile, so a code change does not re-resolve them.
COPY requirements.runtime.lock ./
RUN pip install --no-cache-dir -r requirements.runtime.lock

COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config
RUN pip install --no-cache-dir -e . --no-deps

# The data directory holds the briefings the bot serves. Mount it, or let the container
# start empty and pull from the repository at build time — the bot answers from whatever
# is in data/briefings.
COPY data ./data

# Long-polling, so no port is exposed and no inbound traffic is needed.
CMD ["python", "-u", "-m", "app.jobs.serve_bot"]
