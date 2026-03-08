#!/usr/bin/env bash
set -euo pipefail
for c in $(docker ps --format '{{.Names}} {{.Image}}' | awk '/vllm/{print $1}'); do
  echo "--- $c"
  docker inspect "$c" | sed -n '/"Mounts": \[/, /\],/p' | sed -n '/"Source"\|"Destination"/p'
done
