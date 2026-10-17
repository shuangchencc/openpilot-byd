#!/bin/sh
# Run only in a local Linux build VM. No root/system changes: private user,
# mount and network namespaces; only null/random nodes are visible in /dev.
set -eu
if [ "${P2_IN_NAMESPACE:-}" != 1 ]; then
  exec unshare -Urnm env P2_IN_NAMESPACE=1 "$0" "$@"
fi
mount --make-rprivate /
task_dev=$(mktemp -d "${TMPDIR:-/tmp}/p2-empty-dev.XXXXXX")
for stream in null random urandom; do
  touch "$task_dev/$stream"
  mount --bind "/dev/$stream" "$task_dev/$stream"
done
mount --rbind "$task_dev" /dev
export P2_NETWORK_DEVICE_SANDBOX=linux-user-net-mount
exec "$@"
