# Outreach dashboard for Render (or any Docker host).
# Data lives on a persistent disk mounted at /var/data (see render.yaml); the image holds only code.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Node.js + Claude Code CLI, only for the optional "Find new websites with Claude" button.
# Build with --build-arg INSTALL_CLAUDE=0 to leave it out.
ARG INSTALL_CLAUDE=1
RUN if [ "$INSTALL_CLAUDE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates && \
      npm install -g @anthropic-ai/claude-code && npm cache clean --force && \
      apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY static ./static

# Run as a normal user; /var/data is the persistent disk.
RUN useradd --create-home appuser && mkdir -p /var/data && chown -R appuser /var/data /app
USER appuser

ENV DATA_DIR=/var/data \
    OUTPUT_DIR=/var/data/output \
    GMAIL_TOKEN_FILE=/var/data/credentials/gmail_token.json \
    GMAIL_CLIENT_SECRET_FILE=/var/data/credentials/client_secret.json

EXPOSE 10000
# --proxy-headers: Render terminates HTTPS in front of the app, so the app must trust its forwarded headers.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
