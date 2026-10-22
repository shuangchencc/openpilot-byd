#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

# models get lower priority than ui
# - ui is ~5ms
# - modeld is 20ms
# - DM is 10ms
# in order to run ui at 60fps (16.67ms), we need to allow
# it to preempt the model workloads. we have enough
# headroom for this until ui is moved to the CPU.
export QCOM_PRIORITY=12

# PINNED, deliberately, to the AGNOS already on the target device. Upstream wants 19.9.
#
# launch_chffrplus.sh compares this against /VERSION with a plain string !=, and on a
# mismatch it does not warn - it reboots into the AGNOS updater and loops there until the
# two agree. So shipping upstream's value would force an AGNOS flash on first launch, and
# that flash writes the bootloader partitions (xbl/abl) into the inactive slot.
#
# On this device that flash currently has no verified fallback: slot B is not a trustworthy
# rollback (its success bit is set but it has never been validated), the sp098 rollback is
# already blocked by the redPanda bootstub, there is no verified EDL/QDL recovery path, and
# the backup GPT entries on the LUN holding abl/boot/devcfg have a CRC error. Pinning keeps
# the boot chain untouched: the worst case becomes "openpilot does not start", which is one
# line to undo, instead of "the device does not boot".
#
# Consequence to be aware of: with this pin the device will never take an AGNOS update from
# this tree. Running a 0.11.x userspace on AGNOS 12 is NOT an upstream-supported combination
# - the Adreno/tinygrad model compilation is the most likely thing to break. Raise the pin
# only after a verified recovery path and a verified off-device backup exist.
#
# See migration-audit/independent-stack-prereqs-20261022/STAGE0-RESULT.txt
if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="12.4"
fi

export STAGING_ROOT="/data/safe_staging"
