#!/bin/sh
set -e

ARTIFACTS_DIR="${ARTIFACTS_DIR:-/data/artifacts}"

# init is idempotent: it skips artifacts that already exist (refresh --force replaces them).
# Its JSON summary goes to stderr so it never corrupts the serve-stdio protocol stream,
# and --no-save-dotenv keeps credentials from being written to disk.
if [ "${INIT_ON_START:-true}" != "true" ]; then
  echo "INIT_ON_START=${INIT_ON_START}: skipping spec download" >&2
elif [ ! -w "$ARTIFACTS_DIR" ]; then
  # With a read-only root filesystem and no volume mounted here, every download
  # would be fetched and then fail to write. Skip straight to the bundled specs.
  echo "ARTIFACTS_DIR=${ARTIFACTS_DIR} is not writable: skipping spec download," \
       "serving bundled specs. Mount a writable volume there to fetch fresh specs." >&2
else
  nutanix-mcp --no-save-dotenv init >&2 \
    || echo "init failed; falling back to existing or bundled specs" >&2
fi

# The image CMD is "serve-http". A deployment that overrides args with an empty
# list would otherwise fall through to `nutanix-mcp run`, which validates startup,
# prints a JSON summary and exits 0 — a server container that reports Completed.
if [ "$#" -eq 0 ]; then
  echo "no subcommand given: defaulting to serve-http" >&2
  set -- serve-http
fi

exec nutanix-mcp "$@"
