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

- The current user request authorizes P6 offline F4 capability integration,
  composite dual-board faults and safety regression on independent P0–P5
  patches. Replay from pinned sources, build the real Linux daemon only for its
  explicit offline entry, and verify retained single-H7/SPI behavior with fake
  I/O. No C3XL access, hardware constructors/enumeration, real CAN TX, system or
  slot changes, reset, flashing, or bootstub work. Formal validation must deny
  network and device access. P5's F413 DOS BSP remains an unconfirmed reference
  candidate; artifacts and profiles stay nondeployable. Preserve prior reports.
  The design/inspection statements below describe earlier authorized phases.
- Current work is local DOS/F4 and USB dual-Panda compatibility design against
  the locked official baseline. Do not connect to C3XL or operate hardware as
  part of this design work. The user cannot provide photos/vendor material;
  retain unknown physical identities without blocking local design/mock work
  or treating exterior similarity as proof. See
  `hardware/design/dos-usb-dual-panda/DESIGN.md` for the current plan. Planned
  implementation/test cases are not completed compatibility validation.
- The user has authorized Step 1: connect to the separately powered C3XL via
  existing authenticated SSH for read-only hardware, OS and recovery inspection.
  Keep this an explicit diagnostic action separate from bootstrap/validation.
  Do not write device files/parameters, start or stop existing services, reboot,
  change USB roles, probe I2C, transmit CAN, switch slots, reset or flash Panda,
  update AGNOS, or enter recovery modes. Do not instantiate Panda: its constructor
  performs writes. Use existing keys through SSH without reading their contents.
  Prefer kernel/sysfs, existing logs and bounded passive observations; record
  unknowns when proof would require an action beyond the read-only scope.
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
- The user has clarified that this C3XL was sold without a driver camera and
  telephone/SIM-card functionality. Plan with this product configuration; do not
  treat the missing driver camera as an unexplained fault or ask to start it.
  A cached USB modem interface does not prove usable cellular/SIM functionality.
  See `hardware/step1/followup/RESULT.md` for newer evidence, retaining the earlier
  Step 1 snapshot. Identify external Panda power/ports before connecting it;
  the archived startup wrapper includes automatic flash/reset paths.

## Reproducibility

- Keep hardware evidence inside the repository and verify its hashes and source
  provenance. Research-only material must not become a runtime dependency.
- Keep unredacted large recovery experiments and backups in their existing
  locations. Include only clearly scoped summaries and external archive hashes.
- Run the checks relevant to a change. Full-stack integration is not complete
  merely because the older research decoder or upstream reference tests pass.
- Never publish or push this repository without the user's authorization.
