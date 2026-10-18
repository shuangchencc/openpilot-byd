# P8 application resource isolation

Append this patch to the locked [P5 series](../p5/README.md). The official Panda
pin and the P5 patch/manifest are unchanged. `manifest.json` binds the P5 manifest
hash, P8 patch commit, complete resulting Panda source tree, and validator
inputs. The compiler and reference safety inputs remain inherited from P5.

This patch fixes the H7 candidate's boot-cookie/microphone-buffer alias while
preserving cookie ABI addresses. A dedicated **application-only** linker splits
SRAM4 around the two four-byte cookie reservations, keeps every audio DMA buffer
in SRAM4, and separates the existing 1 KiB H7 stack from static data/heap. F4's
existing 8 KiB reserve gains explicit endpoint checks. Both application linkers
enforce the 136-byte version/signature tail. No physical memory capacity has
been newly inferred.

Build/recheck using the pinned macOS environment:

```sh
upstream-openpilot/.venv/bin/python tools/validate_p8_resources.py --output results/p8-resources-new
```

The output must not exist, including an empty directory; symbolic-link output
ancestors are rejected. Every compiler/test worker runs under the existing
network/device/IOKit-denying Seatbelt profile. The runner reconstructs P5+P8 in
two independent source/output directories, builds F4/H7 debug/release twice,
compares raw/signed/ELF bytes, audits every build and reruns protocol, bxCAN,
FDCAN, BSP, capabilities, selected current safety, and liveness regression.
Map files, logs and compiler callgraph paths are evidence rather than claimed
byte-identical outputs.

The supported candidate builder is `tools/p5/build_candidate.py` in the replayed
Panda tree, now selecting `stm32h7_app.ld` for H7. Generic upstream SCons and its
shared bootstub linker are unchanged and are **not P8 build/installation entry
points**. No bootstub is produced or tested. Both build policies still sign only
with the public development key; neither is a deployment artifact.

The 23 new resource cases include actual exact/overflow linker tests, malformed
stack evidence and the inherited overlap as rejection cases. Known direct
chains are checked and unresolved call sites are reported, but indirect calls,
assembly/exception/FPU costs, interrupt nesting and actual target watermarks
remain unproved. `audit_resources.py --require-runtime-proof` deliberately exits
3 with `BLOCKED_RUNTIME_PROOF` on these candidates. Testing that refusal is not
runtime acceptance; the overall resource deployment gate remains blocked.

Physical identity, installed bootstub/signature compatibility, real audio/DMA
operation and complete worst-case stack proof require later separately scoped
target work. This patch does not authorize C3XL access, reset, flashing, system
changes, or CAN transmission.
