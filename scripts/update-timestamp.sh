#!/usr/bin/env bash
set -euo pipefail

now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

cat > timestamp.json <<EOF
{"timestamp":"$now"}
EOF
