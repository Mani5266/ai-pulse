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

# The briefings the bot serves, and the run history `/status` folds. Only these two: the
# article and event records are the pipeline's working set, reach roughly 90 MB a year,
# and the bot never opens them.
#
# They are baked in at build time, which means the image goes stale the moment the next
# briefing is published. That is deliberate and it is handled outside the container: the
# daily workflow redeploys after it commits, so the running machine carries the briefing
# that was published minutes earlier. A bot that fetched its own data would put a network
# call and a second failure mode inside the reply path to solve a problem the deploy
# already solves.
COPY data/briefings ./data/briefings
COPY data/runs ./data/runs

# Long-polling, so no port is exposed and no inbound traffic is needed.
CMD ["python", "-u", "-m", "app.jobs.serve_bot"]
