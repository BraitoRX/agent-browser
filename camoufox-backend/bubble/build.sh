#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${IMAGE:-agent-browser-camoufox:bubble}"

docker build -t "$IMAGE" -f "$ROOT/camoufox-backend/bubble/Dockerfile" "$ROOT"