#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <CategoryName>"
  exit 1
fi

CATEGORY=$1
DIR="categories/$CATEGORY"
OUT="$DIR/index.json"

if [ ! -d "$DIR" ]; then
  echo "Error: category directory '$DIR' not found"
  exit 1
fi

origins=()
while IFS= read -r json; do
  rel=${json#"$DIR/"}        
  rel=${rel%/cpak.json}      #
  origin=${rel//github\/com/github.com}
  origins+=("$origin")
done < <(find "$DIR" -type f -path "*/cpak.json")

{
  echo "["
  first=true
  for o in "${origins[@]}"; do
    if $first; then
      first=false
    else
      echo ","
    fi
    printf '  "%s"' "$o"
  done
  echo
  echo "]"
} > "$OUT"
