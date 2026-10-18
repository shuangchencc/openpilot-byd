# P6 offline compatibility contracts

This suffix depends on P0–P4 host and the exact independent P5 Panda candidate.
No physical profile is bound. All safety configuration remains noOutput and all
real device/TX/install permissions remain false. No new hardware entry exists.

## F4 capabilities and messages

Every F4 connection must return the exact 16-byte `F4C1` v1 response to read-only
request 0xd5 before either connected board can receive configuration/heartbeat
writes. The pending request is bound to slot, connection token, epoch and group
generation, with an exclusive completion deadline. Missing, short, unknown,
late or mismatched replies cannot activate a session. Reconnection starts a new
observation. H7 uses its pinned upstream ABI and never receives this extension.

Capability validity does not itself make health valid. Effective measurement
availability also requires fresh health. F4 exposes voltage and temperature;
current, sound output and SPI error count are unavailable. Raw health bytes are
retained for evidence. The frozen cereal schema cannot carry null fields, so
unsupported numeric fields are zero placeholders. The explicit profile and
publisher Event.valid (including capability validation) are required together;
message_policy hides F4 current as None. No schema or global H7 semantics change.
A valid F4 power/fan event requires fresh available voltage/fan, not a nonexistent
current sensor. H7 role-selected current remains available.

## Faults and recovery

Control inputs and queues retain generation/control epochs. A disconnect or
replacement cancels old transport state, fragment buffers and both TX queues.
No old message can be replayed after reconnect; fresh configuration and a fresh
disabled-to-enabled edge are still required. Receiving an MCU rejected echo
closes the entire group before subsequent queued TX on either slot. A bounded
immutable rejection record preserves original slot, local bus and connection
provenance for diagnostics; it never becomes a retry request or valid stale CAN.
No alternate-board, alternate-safety-mode or diagnostic fallback is added.

Planned shutdown has at most five scheduler rounds and ten cooperative control
operations, each bounded to 5 ms. This covers surviving F4 re-observation when a
sibling disappears during exit. Cancellation is per handle. SIGINT is simulated
as owner-loop stop intent, not an asynchronous signal handler executing I/O.
An arbitrary backend that violates its timeout contract cannot be preempted by
this single-threaded code. Sudden process/power loss executes no host finally;
separate actual-C virtual-clock tests cover heartbeat expiry while MCU execution
continues. A stopped/unpowered MCU cannot be rescued by a software watchdog.

## Evidence boundary

Native fake-I/O tests exercise the real runtime and cereal loop. Separate C
oracles execute actual pinned safety/queue/heartbeat code with HAL stubs, compare
F4/H7 classic decisions and preserve the intended FD difference. The standalone
single H7/SPI fixture uses one session without a dual-board profile; five original
SPI golden vectors and actual Linux original-source compilation remain required.
These are host tests, not electrical timing, physical watchdog, MCU execution,
full AGNOS operation, Song PLUS safety or road validation.
