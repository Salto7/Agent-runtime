#!/bin/bash
# Worker container entrypoint: `worker` (Dramatiq consumers) or `scheduler` (periodic dream cycles).
set -euo pipefail
case "${1:-worker}" in
  worker)    exec orchestrator worker ;;
  scheduler) exec orchestrator scheduler ;;
  *)         exec "$@" ;;
esac
