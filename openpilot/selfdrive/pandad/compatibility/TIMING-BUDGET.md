# P2 candidate timing and resource budgets

All times below are host monotonic microseconds unless marked ms. Freeze v1,
2026-10-18, against openpilot35e78a9d/pandaa5919516. Numbers are candidate host
policy, not measured C3XL/F4 performance. No live scheduler is installed.

| Path | Bound / threshold | Rationale and tested behavior |
| --- | --- | --- |
| Request read/write | caller1..100ms | finite replacement for timeout0/infinite; illegal0/101 rejected before backend |
| Backend slice | at most5ms | pinned `panda.cc` CAN-send uses5ms; leaves scheduling opportunities within100Hz loop |
| Lock wait | min(request budget,10ms) | one pinned pandad100Hz period; bounded Busy instead of shared-board blocking |
| Wait quantum | at most1ms | cancellation and immediate-zero-timeout retry pacing; no busy loop |
| Cancellation | next boundary; cooperative≤5ms while in backend,≤1ms waiting | permanent local atomic cancellation; no handle close/reset to interrupt IO; fake-clock bound only |
| In-flight / pending queue | 1 / 0 | no accumulated commands or automatic replay; contenders only wait within lock budget; no FIFO fairness claim |
| Control / bulk allocation | 4096 /16384 bytes | explicit bounded caller input; SPI control maximum4032 |
| SPI envelope / chunk / first RX | 4096 /4032 /1013 bytes | locked original `panda_comms.h` and `spi.cc`, not historical2048-byte format |
| SPI poll | ≤32 polls, waits≤100us, shared≤5ms deadline | finite even when fake time stops; polling reply does not resend request |
| Health polling / maximum age | 100000 /250000 | `pandad.cc`100Hz with health every10frames; candidate allows two periods plus50ms margin; missing/future/age≥250000 invalid |
| Heartbeat due / overdue | 100000 /250000 | same original10Hz scheduling; candidate host overdue threshold; NOT the MCU SILENT timeout |

Request/backend/lock/deadline/cancel tests cover threshold−1, threshold,
threshold+1 and exhausted/sub-millisecond remainder. Core clock wrap or backwards
motion fails closed; SPI elapsed subtraction tolerates uint64 wrap, and outer
Transport still rejects a backwards observation. Frozen pure age policy tolerates
one uint64 wrap with elapsed<2^63 and rejects future/ambiguous samples. These are
explicit contracts, not an assumption that all clocks have identical wrap policy.

Health and heartbeat tests exercise pure policy only. P3/P4 must actually apply
the thresholds to collected samples, scheduling, Event validity and group TX
barriers; they do not authorize heartbeats or safety-mode commands in P2. No
buffer from one handle is used by its sibling, but a failed required handle must
still invalidate the whole control group during later integration.

Native libusb open/claim and Linux spidev ioctl may exceed caller expectations
inside OS/driver code. The wrapper detects late completion and preserves counts;
it cannot preempt a stuck syscall. Offline evidence proves bounded algorithmic
attempts and cooperative cancellation with compliant fake IO. Hardware timing,
shutdown under a stuck driver, power loss and MCU heartbeat/SILENT remain untested.
