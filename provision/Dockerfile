FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync_monitors.py monitors.yaml ./

# One-shot tool, not a long-running service: run it on demand via
#   docker compose run --rm provision [--dry-run|--prune]
ENTRYPOINT ["python", "-u", "sync_monitors.py"]
