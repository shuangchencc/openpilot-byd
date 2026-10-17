# P4 offline control session

`ControlSession` is a serialized scheduler over the P3 identity registry. It has
no transport or device permission. `poll` returns at most two commands, one per
connected slot; the caller validates the original command immediately before a
mock dispatch and returns the exact result once. The P4 runtime owns the
transport, message, thermal and final CAN-dispatch boundaries. This class alone
is not a deployed daemon or MCU safety policy.

## Barriers and state

Both bound slots require a current exact 48-byte health packet and three current
exact 64-byte CAN-health packets. Known wire enums/flags, F4 CAN-FD restrictions,
no bus-off/error-warning/error-passive, and the noOutput health contract are
checked before availability. Unknown/short data is not converted to healthy
zeros. A malformed packet clears the corresponding old snapshot; a valid packet
that reports a fault is retained as that actual fault snapshot. Whole-group
publication and simulated CAN admission remain false on either outcome.

Configuration accepts exactly two slot-ordered entries and only the pinned
`noOutput` enum 19. Each slot has its own uint16 parameter and known
alternative-experience bits (2|8|16), with no index-0 replication, implicit ELM327
or allOutput. Every accepted configuration requires a caller-verified fresh
offroad envelope, sends alternative experience then safety, and requires a
post-write health readback of all three values. `controls_allowed` must be false
for noOutput; it is not synthesized to make the simulated host gate pass.

A complete readback group still cannot engage until a fresh disabled sample
followed by a newer enabled sample is observed. Samples retain their source
monotonic time and registry generation. Duplicates, invalid Events, future or
expired timestamps close the group. Reconfiguration, lifecycle changes and
faults erase the prior edge. A frozen enabled state cannot keep heartbeats
engaged. After a fault, a fresh offroad configuration and new disabled/enabled
edge are required; reconnect alone never rearms.

`control_epoch` supplements the P3 registry generation. It changes on every
registry-generation synchronization, fault, accepted reconfiguration and normal
armed-to-disabled transition. The runtime captures both at message ingress and
flushes/rejects old CAN batches/intents across either change. It is not a
cryptographic capability. Public command fields are checked against the exact
single pending command per slot before completion. Command IDs/control epochs
never wrap; test-only seeds initialize an empty scheduler to exercise exhaustion.

## Frozen candidate host budgets

These are deterministic offline scheduling contracts, not measured device or
kernel real-time guarantees. Health/heartbeat constants reuse P2 without changing
its policy tests. P4 additionally rejects clock rollback/wrap permanently.

| Item | Budget / boundary |
| --- | --- |
| Health and each CAN-health polling period | 100,000 us |
| Health and CAN-health valid age | strictly below 250,000 us |
| Heartbeat polling period | 100,000 us |
| Successful heartbeat valid age | strictly below 250,000 us |
| selfdrive source age | strictly below 100,000 us |
| Pending command/completion age | strictly below 100,000 us |
| Pending command capacity | one per slot; no backlog/retry queue |
| Commands returned per poll | at most two, one per connected slot |
| Initial configuration | nine poll rounds: exit safety, exit readback, disengaged heartbeat, alternative experience, safety, health, CAN-health 0/1/2 |
| Fault exit | three poll rounds: noOutput(19,0), exact health readback, disengaged heartbeat |

At the runtime's 10 ms cooperative tick bound, initial configuration needs at
most 90 ms of scheduler rounds, and explicit fault exit needs at most 30 ms,
provided each fake backend honors the finite call budget and the caller runs the
loop. Health and heartbeat deadlines close the host gate at the next owner
inspection. This does not promise preemption of a stalled kernel ioctl.

An uncertain/failed command, expired pending operation, unexpected write reply
or failed exit readback retires that slot's connection token. The same write is
never retried, and its sibling receives its own explicit exit sequence. A
surviving slot is never promoted or used to replay a rejected frame. Normal
disengagement already has verified noOutput on both boards, retains that
configuration and sends an immediate disengaged heartbeat; fault exit explicitly
writes noOutput(19,0) and reads it back before the heartbeat.

Historical nonzero TX-blocked, invalid-RX, overflow, SPI and CAN error counters
are retained. Any subsequent delta, including reset/wrap, and decreasing uptime
close the group instead of claiming a clean current session. Nonzero current
fault/status, heartbeat lost, safety-RX invalid, reset or power-save flags also
close the active simulation barrier. This conservative policy may require a
fresh offline configuration even after an otherwise recoverable condition.

## MCU distinction

The separately compiled pinned firmware oracle proves that a disengaged
heartbeat does not immediately force controls false: firmware uses three 1 Hz
mismatch checks. Its heartbeat timeout independently enters SILENT at the pinned
ignition-dependent counter thresholds, while the software-watchdog fault is a
different path. The host exit sequence therefore does not rely on an immediate
heartbeat side effect. No real MCU watchdog, power loss, C3XL fan behavior,
vehicle safety or target deployment is validated by these host tests.
