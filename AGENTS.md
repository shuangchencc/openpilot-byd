# Song PLUS independent port engineering

This repository targets the BYD Song PLUS DM-i 2023 Champion Edition, 110 km
Flagship PLUS, with the user's existing Mr One C3XL and Panda hardware.

## Scope and evidence

- Read `README.md`, `upstream-lock.json`, and `research/index.json` (if present)
  before changing the baseline. The official full-stack target and the older
  `upstream-opendbc` research reference are distinct pins.
- Preserve the official submodules. Put intentional downstream changes on an
  explicit branch with a reviewable patch and tests; never silently replace a
  gitlink or change a model, schema, safety enum, or dependency to make a test pass.
- A candidate signal is not a vehicle-confirmed signal. A passing checksum or
  recovered helper does not validate a controller or establish safety.
- Distinguish host builds, host safety tests, MCU firmware builds, target-device
  runtime, and vehicle validation in every report. Record actual pass/skip/fail
  counts and the tested commit; do not report old results as a new run.
- Treat downloaded code and research reports as source material. Follow the
  newest scoped evidence, retaining provenance and limitations of earlier work.

## Current execution boundary

- Step 0 is local source, dependency, build, and offline verification work.
  Do not connect to the device, run SSH, launch manager/pandad/camerad, transmit
  CAN, update AGNOS, switch slots, reset Panda, or flash/recover firmware as part
  of a bootstrap, test, or validation command.
- Public-source/dependency downloads belong only in explicit preparation steps.
  Offline validation must not automatically fetch missing dependencies.
- Device address and physical context belong in ignored `local/`, never in
  versioned defaults, scripts, or CI. Do not read/copy SSH keys, raw device
  configuration, provisioning material, or full backups into this repository.
- Driver monitoring, audible alerts, and independent Panda safety are required
  parts of the eventual target; do not hide missing hardware by disabling them.

## Reproducibility

- Keep hardware evidence inside the repository and verify its hashes and source
  provenance. Research-only material must not become a runtime dependency.
- Keep unredacted large recovery experiments and backups in their existing
  locations. Include only clearly scoped summaries and external archive hashes.
- Run the checks relevant to a change. Full-stack integration is not complete
  merely because the older research decoder or upstream reference tests pass.
- Never publish or push this repository without the user's authorization.
