FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run unprivileged. The data volume is owned by this user.
RUN useradd --create-home --uid 10001 kalshi \
    && mkdir -p /app/data && chown -R kalshi:kalshi /app
USER kalshi

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/healthz').read()"

# One worker, because the bot engine holds in-process state (the running
# thread, the opportunity cache, the risk counters). Two workers would mean
# two bots trading the same account with two separate views of exposure.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", \
     "--timeout", "120", "--access-logfile", "-", "app:app"]
