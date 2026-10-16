#!/usr/bin/env bash
# Verify an upstream new-api image before pinning it in chart/values.yaml.
# Images are cosign keyless-signed by QuantumNous/new-api GitHub Actions.
#
# Usage: scripts/verify-image.sh <tag>      e.g. v1.0.0-rc.44
# Prints the multi-arch index digest to pin.
set -euo pipefail

tag=${1:?usage: verify-image.sh <tag>}
ref="docker.io/calciumion/new-api:$tag"
digest=$(crane digest "$ref")
tag_re=$(printf '%s' "$tag" | sed 's/\./\\./g')

cosign verify "docker.io/calciumion/new-api@$digest" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp "^https://github\\.com/QuantumNous/new-api/\\.github/workflows/docker-build\\.yml@refs/tags/${tag_re}\$" \
  > /dev/null

echo "verified $ref"
echo "digest: $digest"
