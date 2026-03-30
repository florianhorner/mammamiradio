ARG BUILD_FROM
FROM ${BUILD_FROM}

# FFmpeg and build deps for Python native extensions
RUN apk add --no-cache ffmpeg build-base libffi-dev

# go-librespot: download pre-built binary for target arch
ARG BUILD_ARCH
RUN set -ex; \
    GL_VERSION="1.0.4"; \
    case "${BUILD_ARCH}" in \
      amd64)   GL_ARCH="linux-amd64" ;; \
      aarch64) GL_ARCH="linux-arm64" ;; \
      *)       GL_ARCH="" ;; \
    esac; \
    if [ -n "${GL_ARCH}" ]; then \
      wget -qO /tmp/gl.tar.gz \
        "https://github.com/devgianlu/go-librespot/releases/download/v${GL_VERSION}/go-librespot_${GL_ARCH}.tar.gz" && \
      tar -xzf /tmp/gl.tar.gz -C /usr/local/bin go-librespot && \
      chmod +x /usr/local/bin/go-librespot && \
      rm /tmp/gl.tar.gz; \
    else \
      echo "Unsupported arch for go-librespot: ${BUILD_ARCH} — Spotify will use fallback audio"; \
    fi

# Copy application
WORKDIR /app
COPY pyproject.toml .
COPY fakeitaliradio/ fakeitaliradio/
COPY radio.toml .
COPY go-librespot/config.yml go-librespot/config.yml
COPY ha-addon/run.sh /run.sh

# Install Python dependencies
RUN pip install --no-cache-dir -e .

RUN chmod +x /run.sh

CMD ["/run.sh"]
