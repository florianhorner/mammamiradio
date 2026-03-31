ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3, FFmpeg, and pip
RUN apk add --no-cache python3 py3-pip ffmpeg

WORKDIR /app

# Copy entrypoint
COPY rootfs/ /

# Copy source and install from local tree
COPY pyproject.toml ./
COPY mammamiradio/ ./mammamiradio/
RUN pip3 install --no-cache-dir --break-system-packages .

# Copy radio.toml default config
COPY radio.toml ./

# Create persistent directories
RUN mkdir -p /data/cache /data/music /data/tmp

# Make run script executable
RUN chmod a+x /run.sh

CMD ["/run.sh"]
