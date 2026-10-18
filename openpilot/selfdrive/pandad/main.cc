#include <cassert>
#include <cstdlib>
#include <cstring>
#include "selfdrive/pandad/compatibility/offline_entry.h"
#include "selfdrive/pandad/compatibility/runtime_integration.h"
#ifdef SONGPLUS_P10_INTEGRATION
#include "selfdrive/pandad/compatibility/production_gate.h"
#endif

#include "selfdrive/pandad/pandad.h"
#include "common/swaglog.h"
#include "common/util.h"
#include "common/hardware/hw.h"

int main(int argc, char *argv[]) {
  // Explicit fixture entry is dispatched before logging, hardware setup, or discovery.
  if (argc == 2 && std::strcmp(argv[1], "--compat-offline") == 0) {
    return openpilot::pandad::compatibility::pandad_compat_offline_main();
  }
#ifdef SONGPLUS_P8_INTEGRATION
  if (argc == 2 && std::strcmp(argv[1], "--compat-runtime-offline") == 0) {
    return openpilot::pandad::compatibility::pandad_runtime_offline_main();
  }
#endif
#ifdef SONGPLUS_P10_INTEGRATION
  if (argc == 2 && std::strcmp(argv[1], "--compat-production-check-offline") == 0) {
    return openpilot::pandad::compatibility::pandad_production_check_offline_main();
  }
#endif
  if (std::getenv("SONGPLUS_PANDA_PROFILE_JSON") != nullptr) return 2;
  for (int i = 1; i < argc; ++i) {
    if (std::strncmp(argv[i], "--compat-", 9) == 0) return 2;
  }
#ifdef SONGPLUS_P10_INTEGRATION
  // The candidate cannot silently fall back to the legacy SPI constructor.
  // Keep this before logging, RT setup, Hardware queries and pandad discovery.
  // A profile/env flag or a passing offline report does not provide authority.
  return openpilot::pandad::compatibility::pandad_production_start_denied_main();
#endif
  LOGW("starting pandad");

  if (!Hardware::PC()) {
    int err;
    err = util::set_realtime_priority(54);
    assert(err == 0);
    err = util::set_core_affinity({3});
    assert(err == 0);
  }

  std::string serial = (argc > 1) ? argv[1] : "";
  pandad_main_thread(serial);
  return 0;
}
