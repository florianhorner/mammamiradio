ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3 and FFmpeg
RUN apk add --no-cache python3 py3-pip ffmpeg

WORKDIR /app

# Copy entrypoint
COPY rootfs/ /

# Install mammamiradio from local source (CI copies it into build context)
COPY pyproject.toml ./
COPY mammamiradio/ ./mammamiradio/
RUN pip3 install --no-cache-dir --break-system-packages .

# Copy radio.toml default config
COPY radio.toml ./

# Create persistent directories
RUN mkdir -p /data/cache /data/music /data/tmp

# Make run script executable
RUN chmod a+x /run.sh

# Validate the install works
RUN python3 -c "import mammamiradio; print('mammamiradio installed OK')"

CMD ["/run.sh"]
