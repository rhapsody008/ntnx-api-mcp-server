#!/bin/sh
set -e
# init is idempotent: it skips artifacts that already exist (refresh --force replaces them).
# Its JSON summary goes to stderr so it never corrupts the serve-stdio protocol stream,
# and --no-save-dotenv keeps credentials from being written to disk.
if [ "${INIT_ON_START:-true}" = "true" ]; then
  nutanix-mcp --no-save-dotenv init >&2 \
    || echo "init failed; falling back to existing or bundled specs" >&2
fi
exec nutanix-mcp "$@"
