ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3, FFmpeg, pip, jq (for options.json), and git (for pip install from repo)
RUN apk add --no-cache python3 py3-pip ffmpeg jq git

WORKDIR /app

# Copy entrypoint
COPY rootfs/ /

# Install mammamiradio from the git repo (source lives at repo root, not in this build context)
RUN pip3 install --no-cache-dir --break-system-packages \
    "mammamiradio @ git+https://github.com/florianhorner/mammamiradio.git"

# Copy radio.toml default config
COPY radio.toml ./

# Create persistent directories
RUN mkdir -p /data/cache /data/music /data/tmp

# Make run script executable
RUN chmod a+x /run.sh

CMD ["/run.sh"]
