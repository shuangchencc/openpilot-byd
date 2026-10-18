# P6 actual-source safety and liveness oracle

`../run_p6_safety.py --panda <P5 tree> --opendbc <pinned tree>
--output <directory> --compiler clang` uses only the Python standard library and
a native C compiler. Linux uses `gcc`. No Panda Python import, device discovery,
CAN endpoint, network operation or firmware build/install is present.

The runner first verifies `source-lock.json`, then includes the unchanged current
opendbc safety headers, P5 `can_common.h` and `can_comms.h`. It extracts exact,
byte-identical source ranges for `set_safety_mode`, `tick_handler`, heartbeat
handling and timestamp arithmetic. It includes the actual fault and software
watchdog headers. Source ranges, full input hashes, compiler commands, native
binary hashes and observed decisions are recorded in `result.json`.

The native matrix is F4/H7 × debug/non-debug: 19 debug groups and 13 non-debug
groups per MCU policy, 64 groups and 5,086 assertions per host platform. Thirty
classic-frame/liveness traces must agree exactly between F4/H7. The FD capability
case intentionally differs. Four native binaries are compiled with warnings as
errors and ASan/UBSan. Each case starts a new process, resetting static firmware
state. This executes host C with MCU policy defines; it is not an ARM emulator.
P5 separately supplies actual ARM builds and low-level controller tests.

| Evidence | Scope |
| --- | --- |
| `deny_modes_all_local_and_host_buses` | SILENT/noOutput reject every bus field 0–7 and DLC 0–15 even with an injected controls-allowed state. |
| `checksum_and_response_flags_cannot_bypass` | Transport checksum, response flags, reserved bus and invalid standard address fail before CAN queue entry. |
| `rejected_frame_retains_local_bus` | Genuine rejected echoes preserve original local bus/address and checksum; all local TX queues remain empty. Host sibling-board non-bypass is established by the companion P6 fault suite. |
| `partial_packet_reset_does_not_send` | Real stream reset discards the partial packet; one fresh allowed ELM327 test vector queues exactly once. This is a positive control, not a production diagnostic bypass. |
| BYD debug groups | Current ATTO3-reference whitelist, correct-bus/DLC positive control, bad RX bus/length, payload checksum, fifth wrong counter, exact 1 s lag boundary/wrap, relay and forwarding restrictions, disengaged steering. Checksum/counter/lag failures also reject a real steering TX attempt. |
| Legacy/non-debug groups | Enum 32 + param 33 is absent; actual firmware falls back to SILENT. Current enum 35 is unavailable without ALLOW_DEBUG and also falls back to SILENT. |
| Abrupt process/USB power loss | No further host commands or cleanup execute. A separately powered, scheduled MCU reaches SILENT at the actual 5/2 decimated tick thresholds and clears all three pending TX queues. |
| MCU halt/power loss | No software action executes while MCU execution stops. A resumed kick records a late-loop fault; it does not itself set SILENT or prove hardware-watchdog recovery. |

Important boundaries:

- Current debug `byd_init` ignores its parameter, so enum 35 + param 33 is
  accepted with ATTO3 semantics. It does **not** recover private legacy enum 32
  semantics or establish Song PLUS support. The host profile must remain unbound.
- Register/peripheral functions and `process_can` are explicit HAL stubs. H7
  queue section names receive a Mach-O segment prefix on macOS only; this does
  not validate MCU memory placement. Actual F4/H7 resource maps remain P5 evidence.
- RX injection and pre-existing controls/queue state are labelled test inputs.
  No reimplemented safety policy, fake safety acceptance or spoofed production
  RX path replaces actual hooks.
- The timer schedule is explicit. Heartbeat counts are not exact wall-clock
  expiry guarantees. Physical CAN completion, relay state, power rails and
  cooling remain untested.
- The P6 orchestrator requires exact nonempty inventories, counts, source hashes,
  executable hashes and source ranges, then recomputes F4/H7 and macOS/Linux
  decision equality. A stale or empty report cannot satisfy the gate.
