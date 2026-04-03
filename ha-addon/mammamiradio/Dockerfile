ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3, FFmpeg, and pip
RUN apk add --no-cache python3 py3-pip ffmpeg

# Install go-librespot for Spotify Connect support
ARG BUILD_ARCH
RUN set -eux; \
    GL_VERSION="0.7.1"; \
    case "${BUILD_ARCH}" in \
        amd64)  GL_ARCH="x86_64"; GL_SHA256="45d6f95c1b21b6ac1d81c368f01730b32174c3b6a09ea3c4066f6fd288ee68c7" ;; \
        aarch64) GL_ARCH="arm64"; GL_SHA256="36f1b9e88372936d1a405ae669d09f3a4e8a25ecdb41b286b69b39c2f5f83762" ;; \
        *)      echo "Unsupported arch: ${BUILD_ARCH}" && exit 1 ;; \
    esac; \
    wget -qO /tmp/go-librespot.tar.gz \
        "https://github.com/devgianlu/go-librespot/releases/download/v${GL_VERSION}/go-librespot_linux_${GL_ARCH}.tar.gz" && \
    echo "${GL_SHA256}  /tmp/go-librespot.tar.gz" | sha256sum -c - && \
    tar -xzf /tmp/go-librespot.tar.gz -C /usr/local/bin go-librespot && \
    chmod +x /usr/local/bin/go-librespot && \
    rm /tmp/go-librespot.tar.gz

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

# Stage default go-librespot config (run.sh copies to /data/ only if missing)
COPY go-librespot-config.yml /defaults/go-librespot-config.yml

# Make run script executable
RUN chmod a+x /run.sh

# Validate the install works
RUN python3 -c "import mammamiradio; print('mammamiradio installed OK')"

CMD ["/run.sh"]
