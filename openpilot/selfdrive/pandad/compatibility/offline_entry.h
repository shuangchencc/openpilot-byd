#pragma once
namespace openpilot::pandad::compatibility {
// Bounded, deterministic fixture entry used by the real pandad executable and
// standalone macOS host build. Only in-memory fake Backend objects are created.
int pandad_compat_offline_main();
}
