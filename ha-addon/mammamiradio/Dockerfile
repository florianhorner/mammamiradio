ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3.11, FFmpeg, and pip
RUN apk add --no-cache python3 py3-pip ffmpeg

WORKDIR /app

# Copy application
COPY rootfs/ /
COPY mammamiradio/ mammamiradio/
COPY pyproject.toml radio.toml ./

# Install Python dependencies
RUN pip3 install --no-cache-dir --break-system-packages .

# Create persistent directories
RUN mkdir -p /data/cache /data/music /data/tmp

# Make run script executable
RUN chmod a+x /run.sh

CMD ["/run.sh"]
