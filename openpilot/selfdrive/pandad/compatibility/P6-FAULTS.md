# P6 compound faults and shutdown evidence

This acceptance uses real `PandadRuntime`, `PandadLoop`, `ControlSession`,
`CanRouter`, `Transport`, cereal serialization and a deterministic fake Backend.
Its positive controls dispatch bytes into fake board backends before injecting
faults. The synthetic profile continues to require two fixed slots and noOutput
on each board. Device access, installation and real CAN permissions remain false.
A successful fake bulk write is evidence of host routing only, not of MCU safety
acceptance or physical CAN transmission.

## Behavior changes

- A fully validated RX frame marked MCU-rejected increments the router's
  saturating `safety_rejections` counter and retains bounded immutable evidence:
  original slot, epoch, connection token, group generation, local bus, host
  rejected-source number, address and receive time. It is not a TX capability.
  The runtime closes the whole control group before either queue can dispatch
  another command. Saturation cannot conceal a subsequent rejection. Pending
  RX/TX is retired; the rejected packet is not republished as fresh CAN after
  the epoch changes. Existing faults from checksum, parser and RX overflow also
  retire registry availability before the next transmit snapshot.
- A TX queue or batch overflow closes the runtime barrier and drops both slot
  queues. It cannot leave earlier commands waiting to transmit after the failed
  enqueue. Other malformed ingress keeps its preexisting rejection behavior.
- Planned shutdown allows five scheduler rounds, at most one control operation
  per slot per round. Every transport operation has a 5 ms cooperative limit:
  at most ten operations / 50 ms of transport budget. A sibling failure changes
  the group epoch; a surviving F4 may need capability read, noOutput, health
  readback, then heartbeat(false). Five rounds cover that sequence without
  retrying the failed board. All remaining transports are then cancelled.
  This is a cooperative scheduling bound, not a kernel or hardware guarantee.
- `PandadLoop::request_shutdown()` is an owner-thread handoff. The next cycle
  runs bounded shutdown before reading the input backlog or performing CAN I/O,
  then publishes invalid observations. The method is not async-signal-safe and
  does not install a handler. The SIGINT test models delivery to the serialized
  owner; an actual signal handler would only set a signal-safe flag.

## Acceptance inventory

`tests/test_p6_faults.cc` contains 22 independent groups. Every scenario that
could otherwise pass because the gate was permanently closed first establishes
readiness, a fresh disabled/enabled edge, and positive dispatch on both slots.

| Frozen case | Evidence in this suite |
|---|---|
| A62 compound faults | checksum corruption together with queued disconnect and stale-health conditions, fresh reconnect, dropped/replaced board, old envelopes and fragments, TX/RX overflow, expiry boundary, bounded delayed read, same-cycle fault before sibling TX |
| A64 no bypass | actual-firmware golden rejected frame with original slot/bus/epoch evidence, both queues closed, partial uncertain write never replayed, relay fault, FWD-blocked counter, safety readback mismatch, noOutput-only configuration and allowed control-write audit |
| A65 exit boundaries | engaged normal shutdown, each slot blocked or cancelled, actual transport mutex contention with an in-flight thread, serialized simulated SIGINT, idempotent shutdown, abrupt scheduler death, simultaneous power removal and fresh recovery |

The golden rejection bytes `8209190000920000000000000000` represent address
0x321, local bus 1, eight zero payload bytes, rejected=true. They are independently
emitted by the actual-source P6 firmware/safety oracle's
`rejected_frame_retains_local_bus` case for F4/H7. The host test asserts source
193 (slot 0) or 197 (slot 1) without changing the frame's local bus or address.
The host never converts the rejection into a receive success, diagnostic
request, alternate safety mode or sibling transmission.

Abrupt loss deliberately performs zero further host calls and makes no claim
that shutdown/finally executed, that powered-off board outputs were observed,
or that a host test establishes a hardware watchdog. MCU heartbeat expiry and
relay/safety behavior are separate actual-source oracle evidence. Empty known
zero-byte read timeouts remain benign; an arbitrary timeout does not itself
mean a board is disconnected.

## Single H7/SPI regression

`tests/test_p6_single_spi.cc` is a separate five-group fixture over one
`SpiIo` fake, the existing `SpiBackend`, `Transport`, and CAN codec. It never
constructs a dual-board profile or substitutes a missing DOS with a second H7.
It positively exercises current eight-byte protocol hashes, 48-byte health,
64-byte CAN health, noOutput and heartbeat writes, classic CAN and full 64-byte
CAN FD round trips with returned-frame provenance. H7 never issues F4 0xd5.
NACK, bad CRC and local cancellation prohibit replay or transport fallback.

A63 additionally requires the fresh existing SPI wire-oracle tests against the
unchanged original SPI definitions, the retained single-H7/Linux daemon source
compile/link, the real offline entry, and current safety regressions. This
fixture does not execute the legacy device-opening path. Linux and macOS
results are reported by the outer P6 runner; no prior run is reused as new
acceptance evidence.
