#!/usr/bin/env bash
set -euo pipefail

# Usage: MODEL_URI=gs://bucket/model-v1.pt ./scripts/download_model_vm.sh [destination]
destination="${1:-./models/model.pt}"
: "${MODEL_URI:?Set MODEL_URI, for example gs://bucket/model-v1.pt}"

mkdir -p "$(dirname "$destination")"
gsutil cp "$MODEL_URI" "$destination"
test -s "$destination"
printf 'Downloaded %s (%s bytes) to %s\n' "$MODEL_URI" "$(stat -c '%s' "$destination")" "$destination"