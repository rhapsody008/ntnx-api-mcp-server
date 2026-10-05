# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
# Editable install on purpose: Settings.project_root resolves relative to
# src/config/settings.py, so a regular install would move the bundled
# default_specs path into site-packages.
RUN pip install -e .

# Bake latest-release specs as the bundled fallback (needs internet at build time).
# Build with --build-arg BAKE_SPECS=false for an offline build.
ARG BAKE_SPECS=true
RUN if [ "$BAKE_SPECS" = "true" ]; then \
      ARTIFACTS_DIR=/app/src/artifacts/default_specs LOG_DIR=/tmp/logs \
        nutanix-mcp --no-save-dotenv init || echo "spec bake failed; image has no bundled specs" >&2; \
      rm -rf /tmp/logs; \
    fi

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin mcp \
 && mkdir -p /data/artifacts /data/logs \
 && chown -R mcp:mcp /data /app \
 && chmod 0755 /usr/local/bin/entrypoint.sh

USER 10001
ENV ARTIFACTS_DIR=/data/artifacts LOG_DIR=/data/logs LOG_FORMAT=json \
    MCP_HTTP_HOST=0.0.0.0 MCP_HTTP_PORT=8000 MCP_HTTP_PATH=/mcp
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve-http"]
