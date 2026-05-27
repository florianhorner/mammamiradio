FROM python:3.14-slim

# Install FFmpeg for audio processing
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install dependencies first for better layer caching
COPY pyproject.toml .
COPY mammamiradio/ mammamiradio/
COPY radio.toml .
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN pip install --no-cache-dir . \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# Create non-root user and directories
RUN useradd -r -s /bin/false radio \
    && mkdir -p /data/cache /data/music /data/tmp \
    && chown -R radio:radio /app /data

ENV MAMMAMIRADIO_BIND_HOST=0.0.0.0
ENV MAMMAMIRADIO_PORT=8000

USER radio
EXPOSE 8000

# Standalone entrypoint — HA add-on overrides this with run.sh.
# The entrypoint auto-generates ADMIN_TOKEN if unset (persisted to /data/admin_token).
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "mammamiradio.main:app", "--host", "0.0.0.0", "--port", "8000"]
