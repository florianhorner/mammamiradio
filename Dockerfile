FROM python:3.11-slim

# Install FFmpeg for audio processing
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install dependencies first for better layer caching
COPY pyproject.toml .
COPY mammamiradio/ mammamiradio/
COPY radio.toml .
RUN pip install --no-cache-dir .

# Create default directories for cache, music, and temp files
RUN mkdir -p /data/cache /data/music /data/tmp

# Default config: point cache/tmp at persistent /data
ENV MAMMAMIRADIO_BIND_HOST=0.0.0.0
ENV MAMMAMIRADIO_PORT=8000

EXPOSE 8000

# Standalone entrypoint — HA add-on overrides this with run.sh
CMD ["python", "-m", "uvicorn", "mammamiradio.main:app", "--host", "0.0.0.0", "--port", "8000"]
