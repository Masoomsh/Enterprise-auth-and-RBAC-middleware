# NOTE: not built in CI yet; validate with `docker compose build` before relying on it.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# python3-saml needs xmlsec; build tools are removed again after the install.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gcc pkg-config libxml2-dev libxmlsec1-dev libxmlsec1-openssl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY semantic_router ./semantic_router
COPY policies ./policies
COPY scripts ./scripts

# Run as an unprivileged user; /srv/database and /srv/data are the only writable paths.
RUN useradd --system --uid 10001 gateway \
 && mkdir -p /srv/database \
 && chown gateway /srv/database
USER gateway

EXPOSE 8000
# --proxy-headers so audited client IPs come from the proxy; restrict who may set them.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
