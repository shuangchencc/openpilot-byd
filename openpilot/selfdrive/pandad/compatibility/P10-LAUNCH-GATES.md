# P10 production admission boundary

P10 adds an executable denial boundary to the candidate's real `main.cc`.
`SONGPLUS_P10_INTEGRATION` is enabled by the candidate SConscript. The default
entry returns 3 before logging, real-time setup, `Hardware` queries, discovery,
or the legacy SPI constructor. There is no native production launcher or
production evidence verifier in this stage. An unknown physical profile remains
UNBOUND; neither an environment variable nor a JSON field can unlock startup.

The existing explicit `--compat-offline` and `--compat-runtime-offline` tests
remain available. New `--compat-production-check-offline` returns 0 when the
inert default check correctly reports blocked admission. Its success is success
of a diagnostic, never authorization to start. It performs no device discovery,
file/config loading, kernel I/O, control writes, CAN transmission, or installation.
Unsupported `--compat-*` arguments still return 2.
The offline acceptance executes the SConscript build contract and manually links
the actual Linux daemon entry; it does not claim an actual full SCons build.

## Review is separate from authority

`evaluate_production_gate` is a pure readiness evaluator. It always returns
false for device access, runtime writes, CAN TX, deployment and production start
authorization, even when injected synthetic evidence satisfies every check.
This makes an offline positive test useful without manufacturing a deployment
capability. P10 does not implement post-handoff admission or continuous
revocation of production authority.

| Requested review | Required evidence, in addition to the preceding row |
| --- | --- |
| Offline diagnostics | No hardware evidence; every authority remains false. |
| Native read-only observation | Valid native binding, locked release, physical identity, recovery chain, power/topology, exclusive ownership, peripheral roles, bounded discovery scheduling and kernel I/O deadline evidence. USB also needs the current active interface; SPI needs configuration and cross-process freshness. |
| Native noOutput runtime | Resource bounds, complete-group health/safety barrier, heartbeat and shutdown behavior. This purpose is a bench review, not driver assistance. |
| Vehicle control | Working driver monitoring, audible alerts, validated vehicle signals/controller, independent Panda safety and closed-course results. |
| Deployment | Installation and rollback evidence. The default production diagnostic requests this full scope. |

Evidence references have bounded shape, unique requirement IDs and short-lived
attestation windows. The verifier must read the referenced content and associate
it with the entire current context: exact board/MCU/capability/role tuple, full
application signatures, release lock, process epoch, discovery revision and
retained leases. A digest, a `true` value, an offline report or a caller-supplied
timestamp is not verification. No real verifier is supplied by P10. Historical
physical evidence may be re-attested for a current session only after this
association is independently verified; acquisition times must remain intact.

Both board observations must be real completed-probe timestamps, no older than
the locked `kLoopMaxGapUs` value (250,000 us), and neither may be future-dated.
The evaluator never re-stamps stale evidence. Every new evaluation re-runs the
verifiers; there is no cached success. A previous review result is unusable after
handoff, ownership/process/revision changes, retirement or cancellation.

## SPI live proof

The serialized executor supplies `LiveSpiProofReader` for each fixed SPI slot.
It must call `SpiFreshnessBackend::proof()` on the still-owned, live backend;
copied JSON or a saved discovery result cannot replace this operation. The gate
checks the expected UID and lease nonce, two distinct nonzero fence IDs, actual
fence completion time and the still-held same-fd lifetime flock. The current
process/revision/context is also passed to the callback and evidence verifier.
Missing callbacks, owner retirement and exceptions deny review.

An old fence can remain valid while the same descriptor and exclusive lock are
continuously held; the board identity observations must still be re-probed on
schedule. A local cancellation invalidates proof. The fence proves behavior only
under the locked firmware's one-entry `(request ID, CRC)` cache and cooperative
ownership of every master using that physical SPI node. It does not prove target
kernel deadlines, physical identity, power stability or absence of an unrelated
master. The proof's `kernel_deadline_proven` remains false and cannot substitute
for independently verified timing evidence.

## Remaining physical work

Before considering any actual native startup, verify the two physical identities,
board/firmware/ABI and topology, safe recovery, current USB interface or exact SPI
node/configuration, exclusive ownership, target worst-case I/O latency and worker
cleanup. Verify resource/stack bounds, health/safety and heartbeat/exit faults.
Vehicle operation additionally requires a driver-monitoring solution (the C3XL's
missing camera is not a bypass), audible alerts, independently confirmed Song
PLUS signals/controller/safety and controlled validation. Passing the offline
matrix supplies none of these physical facts.
