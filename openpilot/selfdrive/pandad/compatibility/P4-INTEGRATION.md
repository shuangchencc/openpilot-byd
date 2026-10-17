# P4: explicit pandad offline integration

The locked official default remains the single-SPI device path. The same Linux
`pandad` executable now has an explicit `--compat-offline` entry dispatched before
logging, priority setup or hardware discovery. It runs bounded synthetic fixtures
through the shared `PandadRuntime`, P2 Transport over in-memory fake Backends,
P3 routing, the P4 control session and the unchanged cereal wire schema. It exits;
it does not subscribe to live messaging or construct physical Panda/USB handles.
This is an offline integration build, not an installation or vehicle runtime.

The Python wrapper dispatches the same explicit entry before importing Panda or
hardware. Supplying `SONGPLUS_PANDA_PROFILE_JSON` to the ordinary wrapper/native
entry fails closed rather than entering the old automatic reset/flash path.
The four Python consumers use that variable only for a strict P0 synthetic
profile. Invalid/unbound input cannot fall back to the official single-board
policy. All profiles still deny real device access, CAN TX and firmware install.

## Boundaries and message ownership

The serialized owner captures `(registry generation, control epoch)` before
reading/deserializing an event. Delayed old input cannot be assigned a fresh
epoch. Configuration, fault recovery and disengagement retire control epochs,
flush the routing queues, and require new enabled-state observations. Epoch is
local metadata, not a cereal schema field or an authenticated security token.
The native serializer and all consumers must use the same immutable profile.

`pandaStates` always has two slots. A missing/stale/invalid slot invalidates the
whole Event; unknown placeholders explicitly report unknown type and lost
heartbeat. Available fault data can remain diagnostic but cannot make the Event
valid. Valid output timestamps preserve the oldest required health/CAN-health
observation, so republication cannot refresh an old snapshot. CAN and peripheral
messages likewise retain their original receive/observation age.

`CarParams.safetyConfigs` must contain exactly two explicit noOutput entries;
parameters are per slot and alternativeExperience follows the unchanged shared
CarParams field. No implicit ELM327, firmware query, OBD switch, allOutput or
vehicle-specific safety selection is introduced. The noOutput gate permits only
simulated dispatch in the fake backend; actual noOutput MCU safety forbids CAN TX.

## Scheduler and resource policy

The owner serializes callbacks, input admission and dispatch. Each `tick` issues
at most one bounded control request per slot through P2, with a 5 ms cooperative
budget per request. It checks freshness before issuing commands and validates
command id/token/epoch/deadline again at completion. A failed or uncertain write
retires that handle without replay. A blocked board therefore does not drain a
queue or consume an unbounded retry loop ahead of its sibling. Hardware/kernel
stall preemption is not claimed.

| Item | Candidate offline policy |
| --- | --- |
| Health / CAN-health validity | age <250 ms, each board and each of its three buses |
| Heartbeat | 100 ms schedule, completion age <250 ms |
| SelfdriveState / sendcan | age <100 ms, original source timestamp |
| P3 TX queue residence | <20 ms; max64 frames per slot |
| DeviceState / fan demand | <750 ms, accommodating pinned 2 Hz producer |
| Fan RPM observation | <250 ms; must be observed before simulated enable |
| Camera demand when IR supported | <750 ms; unsupported IR produces no write |
| Main control opportunity | candidate 10 ms; two control calls total <=10 ms if backends honor contract |
| Fault exit | noOutput write, exact readback, disengaged heartbeat; three bounded opportunities |
| Planned shutdown | three bounded exit rounds, then cancel handles/retire group |

The reusable `PandadLoop` executes control work first, accepts at most16 captured
ingress envelopes, then gives each board one CAN read/write opportunity. It
schedules peripheral work at20Hz, pandaStates at10Hz and peripheralState at2Hz.
The 10ms cycle budget is checked between cooperative operations; a budget overrun
or cycle gap >=250ms closes the group and publishes invalid observations.
Input/sink exceptions also close the group. No socket or physical device factory
is provided in this synthetic-only stage.
These fake-clock budgets are not C3XL timing measurements. Main-loop starvation
fails closed on the next observation; abrupt process/power loss instead depends
on the separately tested MCU heartbeat timeout. The original MCU watchdog does
not run itself when its loop is stopped, and its fault is not immediate SILENT.

## Peripheral and recovery policy

Ignition is the OR of configured sources. A local DOS ignition=false cannot
power down another live ignition source. Harness, power observations and fan/IR
commands use their explicit roles and capabilities. A peripheral Event uses a
single snapshot: fan RPM comes from the fan role and voltage/current from the
power-monitor role, even when those roles belong to different boards. Its source
time is the older of those observations; missing required values invalidate the
whole unchanged-schema Event rather than inventing zero-valued sensors. No GPIO or power-save output
is inferred from an unknown physical board. A missing/stale required fan sensor
or deviceState observation closes the group; the code does not fabricate a safe
physical fan output. Driver monitoring remains a separate unresolved hardware
and vehicle requirement; unsupported IR is not permission to disable monitoring.

A fresh transition to offroad immediately closes the control gate, flushes queued
CAN and starts explicit disengagement. That accepted offroad observation is
retained for a subsequent explicit configuration; continued enabled messages
cannot keep heartbeat engaged while offroad. Enabled input also requires a fresh
onroad deviceState.

Recovery requires complete exact identities/new handles after a connection
fault, a fresh offroad deviceState, explicit two-slot configuration and matching
post-write health/CAN-health, fresh heartbeat and required peripheral readings,
then a fresh disabled-to-enabled selfdriveState transition. Connection return
alone never restores engagement. The real Python consumers remain disabled by
P0 permissions even when synthetic messages are otherwise valid.

## What this does not establish

No MCU firmware build, C3XL execution, physical cooling behavior, operating-system
change or vehicle safety validation is included. The default Linux SPI daemon is
compiled and linked for regression, but only the explicit fake-I/O entry is run.
The Python consumer tests execute selected actual functions/branches using real
cereal bytes; they do not launch the full hardwared/card/selfdrived/UI processes.
Real device discovery, profile binding, live Pub/Sub scheduling and deployment
remain gated behind later hardware/safety/integration validation.
