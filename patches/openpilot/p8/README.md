# P8 host runtime integration candidate

This is an additive patch after P6 `d4d610ecb08ab27200e48c07757b147861f710a5`.
The official openpilot/Panda/opendbc/msgq commits and all earlier patches remain
unchanged. No device profile, permission, controller, schema or safety mode is
promoted by this patch.

The new `RuntimeIntegration` owns complete discovery snapshots, exact profile
identity matching, monotonically allocated connection tokens, bounded consumed
handle history, and USB/SPI transport lifetime. It directly drives the existing
P4/P6 `PandadRuntime` and `PandadLoop`. Incomplete, duplicate, mismatched, stale,
future or regressing inventory is rejected before opening new transports. A
changed group requires fresh handles for both slots. A failed partial open
consumes both nonces. Cancellation precedes backend release. Normal shutdown
uses the existing bounded owner-loop exit; destructor cleanup only cancels and
releases and does not claim to deliver shutdown commands.

`InjectedTransportFactory` uses the actual P2 `UsbBackend` and `SpiBackend`.
There is deliberately **no native discovery/I/O provider and no real profile**.
Its injected USB API's deliberate open boundary is exercised against fake APIs;
no hardware enumeration is performed. Descriptor identity is not MCU proof.

`RealtimeSource` and `RealtimeSink` use the unchanged actual cereal schema, topic
validation and the P6 timestamp/epoch checks. A bounded queue fence discards old
messages across connection/control epochs. CarParams requires an explicitly
stamped new submission, never an implicit reload of a persisted value. Topic
mismatch, malformed/oversized data, backlog or publication failure closes the
existing group gate. This candidate rejects `sendcan` and enabled selfdriveState
before the transport; the old simulated P6 positive controls remain in tests.

`msgq_adapter` binds already-open actual `SubSocket`/`PubSocket` interfaces, copies
bounded input, and uses nonblocking receives. It does not create or connect
endpoints. Offline acceptance uses injected memory channels and, on Linux, both an
in-memory SubSocket and seven pairs of actual msgq endpoints in a private
16 MiB tmpfs inside the device/network-denying namespace. Three output topics
and four input topics exercise the actual interfaces; no network sockets or
vehicle endpoints are opened. Target IPC timing/overload remains unverified.

P8 also fixes two P6 receive-path defects exposed by this integration: ordinary
noOutput/disabled transmit attempts used to clear RX queues and partial frames,
and late cabinCameraState samples lacked the control-epoch timestamp lower
bound. Ordinary TX suppression now clears TX only; every fault, disconnect and
epoch barrier retains the previous full clear. Tests cover complete and
fragmented RX in observation mode, reconnect between fragments, and a late
old-camera sample after the fence. Same-handle activation evidence changes
retire the group. The SConscript keeps the pinned libusb dependency local to the
daemon so the original extras target does not acquire a broken library path.

The real Linux pandad links all new modules and dispatches the explicit
`--compat-runtime-offline` unbound fixture when built with the P8 SConscript
flag. The default original SPI path is preserved. `--compat-runtime` and
`--compat-device` remain rejected, with no device access. This is not a runtime
installer, AGNOS image or deployment authorization.

Run from the maintenance repository on the prepared host:

```sh
upstream-openpilot/.venv/bin/python -S tools/validate_p8_runtime.py \
  --output results/p8-runtime-UNIQUE \
  --linux-instance byd-recovery-311 \
  --linux-root /home/shuang.guest/songplus-p2-build
```

The output directory must be new and must not have symlink parents. Both workers
run in the retained network/device-denying sandboxes, with cached dependency
hash checks and source hashes before/after execution. They rebuild the old
P0–P6 native transport/registry/control/capability/single-H7-SPI suites, actual
cereal/runtime/loop/composite-fault suites, then the P8 owner/message fixtures on
the P8-applied tree. Linux additionally links the actual daemon and invokes only
explicit offline entries. No fresh ARM firmware build occurs in this host task.
