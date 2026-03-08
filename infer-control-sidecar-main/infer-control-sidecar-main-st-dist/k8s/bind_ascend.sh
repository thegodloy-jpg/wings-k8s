#!/bin/bash
set -e

NODE=$1
CONTAINER=$2

echo "=== Binding Ascend paths into k3s container: $CONTAINER on $NODE ==="

# Create directories inside k3s container
docker exec $CONTAINER mkdir -p /usr/local/Ascend/driver/lib64
docker exec $CONTAINER mkdir -p /usr/local/Ascend/driver/lib64/common
docker exec $CONTAINER mkdir -p /usr/local/Ascend/driver/lib64/driver

# Get container PID
PID=$(docker inspect --format='{{.State.Pid}}' $CONTAINER)
echo "Container PID: $PID"

# Bind-mount Ascend driver lib64 into container's mount namespace
# Use host's mount binary (not container's) via nsenter with only --mount
nsenter --target $PID --mount /usr/bin/mount --bind /usr/local/Ascend/driver/lib64 /usr/local/Ascend/driver/lib64
echo "Bind mount OK"

# Verify
docker exec $CONTAINER ls /usr/local/Ascend/driver/lib64/
echo "=== Done ==="
