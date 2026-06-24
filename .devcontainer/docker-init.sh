#!/bin/sh

set -e

# Kill leftover dockerd / containerd from a previous run
pkill -9 dockerd 2>/dev/null || :
pkill -9 containerd 2>/dev/null || :
sleep 1

# Clean up old PID files and sockets
find /run /var/run -iname 'docker*.pid' -delete || :
rm -f /var/run/docker.sock /var/run/docker/containerd/containerd.sock || :
rm -f /var/lib/docker/containerd/daemon/io.containerd.metadata.v1.bolt/meta.db-lock 2>/dev/null || :

# Move processes to init cgroup to allow nesting (v2)
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs || :
    sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control || :
fi

# Pick a storage driver that works inside an overlay filesystem (codespaces)
if command -v fuse-overlayfs > /dev/null 2>&1; then
    STORAGE_DRIVER="fuse-overlayfs"
else
    STORAGE_DRIVER="vfs"
fi

# Start the daemon and wait until it is ready
dockerd --storage-driver="$STORAGE_DRIVER" > /tmp/dockerd.log 2>&1 &
echo "Waiting for Docker daemon to start..."
while ! docker info > /dev/null 2>&1; do sleep 1; done
echo "Docker daemon is ready."