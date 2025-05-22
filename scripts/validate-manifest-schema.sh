#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <manifest.json>"
  exit 1
fi

file="$1"

if ! jq empty "$file" >/dev/null 2>&1; then
  echo "ERROR: invalid JSON in $file"
  exit 1
fi

for field in name description; do
  if ! jq -e --arg f "$field" '.[$f] // empty | length > 0' "$file" >/dev/null; then
    echo "ERROR: '$field' is missing or empty in $file"
    exit 1
  fi
done

count=$(jq -r '[.branch, .commit, .release]
  | map(select(. != null and . != "")) | length' "$file")
if [ "$count" -ne 1 ]; then
  echo "ERROR: exactly one of branch, commit or release must be set in $file"
  exit 1
fi

echo "OK: $file"
