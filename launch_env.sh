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

# Follow whatever AGNOS is installed, instead of upstream's fixed 19.9.
#
# launch_chffrplus.sh compares this against /VERSION with a plain string !=, and on a
# mismatch it does not warn: it reboots into the AGNOS updater and loops there until the two
# agree. Shipping any fixed value therefore makes the tree flash AGNOS on first launch, and
# that flash writes the bootloader partitions (xbl/abl) into the inactive slot.
#
# This device is being set up with two usable slots - the vendor stack on AGNOS 12.4 and our
# tree on AGNOS 19.9 - so a fixed value is wrong in one direction or the other: upstream's
# 19.9 would flash over 12.4, and an earlier 12.4 pin here would have flashed 19.9 back down
# to 12.4 once the second slot existed. Reading /VERSION agrees with either slot and so
# never starts a flash from software.
#
# This deliberately gives up automatic AGNOS updates. That is the point: on this device
# flashing AGNOS is a decision a person makes, with a verified recovery path and an
# off-device backup in hand, not something a launch script does on boot. Note also that a
# 0.11.x userspace on AGNOS 12.4 is not an upstream-supported combination, so the two slots
# are not interchangeable - see the stack selector in
# migration-audit/stagec-prep-20261023/continue-selector.sh.
#
# See migration-audit/independent-stack-prereqs-20261022/STAGE0-RESULT.txt
if [ -z "$AGNOS_VERSION" ]; then
  if [ -r /VERSION ]; then
    export AGNOS_VERSION="$(cat /VERSION)"
  else
    export AGNOS_VERSION="19.9"   # upstream's value, for a PC or a missing /VERSION
  fi
fi

export STAGING_ROOT="/data/safe_staging"
