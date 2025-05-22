#!/usr/bin/env bash
set -euo pipefail

DEBUG=${DEBUG:-false}
log_debug() {
    if [[ "$DEBUG" == "true" ]]; then
        echo "DEBUG: $*" >&2
    fi
}

[[ $# -eq 1 ]] || { echo "Usage: $0 <CategoryName>" >&2; exit 1; }

CATEGORY=$1
ROOT="categories/$CATEGORY"
OUT="$ROOT/index.json"

log_debug "CATEGORY=$CATEGORY"
log_debug "ROOT=$ROOT"
log_debug "OUT=$OUT"

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
log_debug "CURRENT_BRANCH=$CURRENT_BRANCH"

if [[ "$CURRENT_BRANCH" != "main" && "$CURRENT_BRANCH" != "master" ]]; then
    echo "ℹ️  You are in branch '$CURRENT_BRANCH'. Ensure this is the correct branch for the PR." >&2
else
    echo "ℹ️  You are in the default branch '$CURRENT_BRANCH'." >&2
fi

[[ -d $ROOT ]] || { echo "❌  category '$CATEGORY' not found" >&2; exit 1; }

mapfile -t manifests < <(
    git ls-files -- ':(glob)'"$ROOT"'/**/manifest.json'
)

if [[ ${#manifests[@]} -eq 0 ]]; then
    echo "⚠️  no manifest.json under $ROOT" >&2
fi

origins=()
for mf in "${manifests[@]}"; do
    rel=${mf#"$ROOT/"}           
    rel=${rel%/manifest.json}
    rel=${rel//github\/com/github.com}
    origins+=("$rel")
done

printf '%s\n' "${origins[@]}" | sort -u | jq -Rsc 'split("\n")[:-1]' > "$OUT"
echo "✅  Written $((${#origins[@]})) rows to $OUT"

# Generate a tree of the directory
echo "📂 Directory tree under $ROOT:"
tree "$ROOT"