# P3 session and router candidate budgets

Frozen for offline implementation against openpilot35e78a9d / pandaa5919516.
These are candidate host limits, not measured C3XL/F4 operating guarantees.
They do not enable vehicle TX, replace MCU safety or define P4 health validity.

| Resource or time | Candidate limit | Rationale / boundary |
| --- | --- | --- |
| Required logical slots | exactly2; IDs0,1 | P0 explicit profile; never compress after disconnect |
| Local CAN / host source | local0,1,2; host0,1,2,4,5,6 | stride4 address space; 3/7 reserved; returned/rejected only RX |
| Connection token | uint64, positive, strictly increasing per identity for each new handle | USB bus/address reuse is not a new trusted session; no unbounded retired-token set |
| Slot epoch / group generation | uint64, checked increment | no counter wrap or reuse; exhaustion closes registry permanently |
| sendcan source age | strictly less than100000us | 10 periods of pinned100Hz pandad; deliberate tightening of upstream's age<1s |
| TX staging residence | strictly less than20000us | at most two100Hz periods; recheck source age and group/token at dequeue |
| RX/frame/fragment age | strictly less than100000us | 10 aggregation periods; first fragment's timestamp is not refreshed by later bytes |
| TX queue | at most64frames per slot | bounded candidate staging, sufficient for one max64-frame input batch; reject overflow atomically |
| TX input batch | at most64frames total | validate every route before committing any frame; no partial enqueue across boards |
| RX queue | at most256frames per slot | bounded candidate buffering; overflow visible and invalidates group |
| CAN bytes per frame / RX feed | at most70 /16384 | reuse P1 exact codec limits; parser residual at most69bytes |
| Diagnostic history | fixed counters/current reason | no unbounded connection history or in-memory event log |

At exactly the age threshold a frame is expired. Future timestamps are rejected
before subtraction. Router clock rollback/wrap invalidates its session and
requires a fresh router; it must never make stale data young. Old callback tokens
are rejected before applying their timestamps, so a stale callback cannot poison
a new session. Delayed publication retains original receive time and age.

P2 Transport has no pending IO queue (capacity0, one in-flight operation). P3's
bounded queues stage offline routing intents and received frames above that
layer. This does not change P2's no-replay behavior or provide an active scheduler.
A dequeued simulation intent is not permission to call an actual device.
TX batches must carry the group generation captured at ingress, including batches
not yet enqueued. A future message adapter must not relabel an old batch with the
current generation. Held TX intents and RX frames have explicit final validation
helpers for generation and age checks; they retain their original timestamps.

Any required-board lifecycle change advances the group generation; the router
clears both boards' queued TX/RX and residual bytes before its next observable
operation. Fixed slots remain reserved. Thus reconnect cannot release commands
queued before the outage, including commands for the surviving board. Reconnect
is structural revalidation only; P4 must separately apply offroad/configuration,
health freshness and per-board safety-readback barriers before any real control.

Malformed current-session RX, RX overflow, expired partial framing or backwards
router time invalidates the group and requires fresh connection tokens. Invalid
TX input/overflow rejects the complete batch with visible counters; it does not
consume/replay data or reroute it through the sibling. Completed RX expiration
is counted; remaining fresh per-board FIFO data may still be published.

All registry/router calls belong to one serialized event loop. P3 owns no IO,
threads or locks and holds no lock while a P2 transfer is in progress. Offline
integration tests explicitly feed fake transport results and report a failure
back to the registry. Production lifecycle notification/scheduling is not wired.

Inventories longer than two entries are rejected before candidate inspection.
Current live handles are retired; uninspected candidate tokens are not registered.
For a two-entry duplicate identity, both known-identity candidate tokens are
consumed. In either case the old live connection and its queued work cannot heal
by merely deleting an extra inventory entry. No enumeration history is retained.
