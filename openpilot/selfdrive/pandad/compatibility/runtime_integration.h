#pragma once
#include <array>
#include <memory>
#include <set>
#include <string>
#include "pandad_loop.h"
#include "realtime_bridge.h"
#include "usb_backend.h"
#include "spi_backend.h"

namespace openpilot::pandad::compatibility {
// A COMPLETE bounded discovery result; not an enumeration implementation.
// Handles are opaque session nonces, never inferred from USB address/position.
struct DiscoveryItem { SessionIdentity identity; std::string handle_nonce; bool usb_alt0_verified = false; std::uint32_t spi_first_request_id = 0; };
struct DiscoverySnapshot { std::uint64_t revision = 0, observed_us = 0; std::vector<DiscoveryItem> items; };
class DiscoveryProvider {
public:
  virtual ~DiscoveryProvider() = default;
  virtual DiscoverySnapshot snapshot() = 0;
};
// This stage intentionally has no native provider. Implementations are supplied
// only by offline fixtures. A real discovery/authentication/recovery boundary is
// separate work; a USB descriptor is not evidence of MCU or application trust.
class OfflineIoProvider {
public:
  virtual ~OfflineIoProvider() = default;
  virtual std::unique_ptr<LibusbApi> usb(const DiscoveryItem &) = 0;
  virtual std::unique_ptr<SpiIo> spi(const DiscoveryItem &) = 0;
};
struct OwnedTransport {
  std::unique_ptr<SpiIo> spi_io;
  std::unique_ptr<Backend> backend;
  std::unique_ptr<Transport> transport;
  ~OwnedTransport() { if (transport) transport->cancel(); }
};
class InjectedTransportFactory {
public:
  InjectedTransportFactory(OfflineIoProvider &provider, Clock &clock) : provider_(provider), clock_(clock) {}
  std::unique_ptr<OwnedTransport> open(const DiscoveryItem &item, const SessionBinding &binding);
private:
  OfflineIoProvider &provider_; Clock &clock_;
};
// Explicit unbound fixture dispatch used by the real pandad executable.
int pandad_runtime_offline_main();

// Single owner for discovery, handle lifetime and the existing P4/P6 scheduler.
// All public methods run on the serialized owner thread. It does not enumerate,
// open sockets, install firmware, or create native I/O implementations.
class RuntimeIntegration {
public:
  RuntimeIntegration(const SessionProfile &, Clock &, DiscoveryProvider &, InjectedTransportFactory &, RealtimeSource &, RealtimeSink &);
  bool refresh();
  LoopCycleResult cycle();
  void request_shutdown() { stopping_ = true; loop_.request_shutdown(); }
  InputEpoch capture() { return runtime_.capture(); }
  ControlSnapshot snapshot() { return runtime_.snapshot(); }
  SessionSnapshot sessions() const { return runtime_.sessions(); }
  const RuntimeCounters &counters() const { return runtime_.counters(); }
private:
  void retire(const char *reason);
  const SessionProfile profile_;
  Clock &clock_; DiscoveryProvider &discovery_; InjectedTransportFactory &factory_;
  PandadRuntime runtime_; PandadLoop loop_;
  std::array<std::unique_ptr<OwnedTransport>,2> handles_;
  std::array<std::string,2> nonces_;
  std::array<std::uint64_t,2> tokens_{};
  std::array<bool,2> usb_alt0_{};
  std::array<std::uint32_t,2> spi_ids_{};
  std::set<std::string> consumed_;
  std::uint64_t revision_ = 0, next_token_ = 0, observed_us_ = 0;
  bool stopping_ = false;
};
}  // namespace openpilot::pandad::compatibility
