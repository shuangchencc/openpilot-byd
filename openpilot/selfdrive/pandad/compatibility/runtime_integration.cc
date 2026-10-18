#include "runtime_integration.h"
#include <limits>
#include <iostream>
#include <stdexcept>
namespace openpilot::pandad::compatibility {
std::unique_ptr<OwnedTransport> InjectedTransportFactory::open(const DiscoveryItem &item, const SessionBinding &binding) {
  if (!(item.identity == binding.expected) || binding.safety_config_semantics != "noOutput" ||
      item.identity.identity.rfind("fixture:",0) != 0) return nullptr;
  auto owned = std::make_unique<OwnedTransport>();
  if (item.identity.transport == "USB") {
    if (!item.usb_alt0_verified) return nullptr;
    auto api = provider_.usb(item);
    if (!api) return nullptr;
    auto usb = std::make_unique<UsbBackend>(std::move(api));
    const auto type = item.identity.board_type == "DOS" ? 6U : 7U;
    // Explicit activation applies ONLY to the caller's injected fake API. No
    // native factory or real profile is reachable through this integration.
    if (usb->open_bound({item.identity.identity,static_cast<std::uint16_t>(type),true},true) != Status::Ok) return nullptr;
    owned->backend = std::move(usb);
  } else if (item.identity.transport == "SPI") {
    if (!item.spi_first_request_id) return nullptr;
    owned->spi_io = provider_.spi(item);
    if (!owned->spi_io) return nullptr;
    owned->backend = std::make_unique<SpiBackend>(*owned->spi_io,clock_,item.spi_first_request_id);
  } else return nullptr;
  owned->transport = std::make_unique<Transport>(*owned->backend,clock_);
  return owned;
}
RuntimeIntegration::RuntimeIntegration(const SessionProfile &profile, Clock &clock, DiscoveryProvider &discovery,
                                       TransportFactory &factory, RealtimeSource &source, RealtimeSink &sink)
  : profile_(profile), clock_(clock), discovery_(discovery), factory_(factory), runtime_(profile,clock), loop_(runtime_,clock,source,sink) {}
void RuntimeIntegration::retire(const char *reason) {
  // Runtime may still hold pointers. Reconcile/cancel BEFORE freeing owners.
  runtime_.invalid_input(reason);
  runtime_.reconcile({},{{nullptr,nullptr}});
  for (auto &handle : handles_) handle.reset();
  nonces_ = {}; tokens_ = {}; usb_alt0_ = {}; spi_ids_ = {};
}
bool RuntimeIntegration::refresh() {
  if (stopping_ || (profile_.binding_status != "SYNTHETIC_BOUND" && profile_.binding_status != "NATIVE_READ_ONLY_BOUND")) return false;
  try {
    const auto inventory = discovery_.snapshot();
    const auto now = clock_.now_us();
    if (!inventory.revision || inventory.revision < revision_ || !inventory.observed_us ||
        inventory.observed_us > now || now-inventory.observed_us >= kLoopMaxGapUs ||
        inventory.observed_us < observed_us_ || inventory.items.size() != 2) {
      retire("DISCOVERY_INVALID_OR_STALE"); return false;
    }
    std::array<const DiscoveryItem *,2> items{};
    for (const auto &item : inventory.items) {
      std::size_t slot = item.identity.identity == profile_.slots[0].expected.identity ? 0 : 1;
      if (!(item.identity == profile_.slots[slot].expected) || items[slot] || item.handle_nonce.empty() ||
          item.handle_nonce.size() > 128) { retire("DISCOVERY_IDENTITY_MISMATCH"); return false; }
      items[slot] = &item;
    }
    if (!items[0] || !items[1] || items[0]->handle_nonce == items[1]->handle_nonce) {
      retire("DISCOVERY_DUPLICATE_HANDLE"); return false;
    }
    const bool same = handles_[0] && handles_[1] && nonces_[0] == items[0]->handle_nonce && nonces_[1] == items[1]->handle_nonce;
    if (inventory.revision == revision_ && !same) { retire("DISCOVERY_REVISION_REUSE"); return false; }
    if (same) {
      for (std::size_t slot=0;slot<2;++slot) {
        if (items[slot]->usb_alt0_verified != usb_alt0_[slot] || items[slot]->spi_first_request_id != spi_ids_[slot]) {
          retire("DISCOVERY_ACTIVATION_EVIDENCE_CHANGED"); return false;
        }
      }
      if (!handles_[0]->transport->healthy() || !handles_[1]->transport->healthy() || !runtime_.sessions().group_available) {
        retire("DISCOVERY_RETIRED_HANDLE"); return false;
      }
      revision_ = inventory.revision; observed_us_ = inventory.observed_us; return true;
    }
    // A changed group retires BOTH owners. No hot swap under an existing token.
    // Demand fresh handle nonces for both; a removed/rejected handle never heals.
    retire("DISCOVERY_GROUP_CHANGED");
    for (const auto *item : items) {
      if (consumed_.count(item->handle_nonce) || consumed_.size() > 126 || next_token_ > UINT64_MAX-2) return false;
    }
    // Reserve both before any opening: a partial failure consumes both handles.
    for (const auto *item : items) consumed_.insert(item->handle_nonce);
    revision_ = inventory.revision; observed_us_ = inventory.observed_us;
    std::vector<SessionObservation> observations;
    for (std::size_t slot=0;slot<2;++slot) {
      tokens_[slot] = ++next_token_;
      handles_[slot] = factory_.open(*items[slot],profile_.slots[slot]);
      if (!handles_[slot]) { retire("DISCOVERY_OPEN_FAILED"); return false; }
      nonces_[slot] = items[slot]->handle_nonce;
      usb_alt0_[slot] = items[slot]->usb_alt0_verified;
      spi_ids_[slot] = items[slot]->spi_first_request_id;
      observations.push_back({items[slot]->identity,tokens_[slot]});
    }
    if (!runtime_.reconcile(observations,{{handles_[0]->transport.get(),handles_[1]->transport.get()}})) {
      retire("DISCOVERY_RECONCILE_FAILED"); return false;
    }
    return true;
  } catch (...) { retire("DISCOVERY_CALLBACK_EXCEPTION"); return false; }
}
LoopCycleResult RuntimeIntegration::cycle() {
  if (!stopping_) refresh();
  auto result = loop_.cycle();
  if (result.shutdown) retire("OWNER_SHUTDOWN");
  return result;
}
int pandad_runtime_offline_main() {
  struct FixtureClock final : Clock { std::uint64_t now_us() override { return 1000000; } void wait_us(std::uint32_t) override {} } clock;
  struct ForbiddenDiscovery final : DiscoveryProvider {
    unsigned calls = 0;
    DiscoverySnapshot snapshot() override { ++calls; throw std::logic_error("unbound discovery invoked"); }
  } discovery;
  struct ForbiddenIo final : OfflineIoProvider {
    unsigned calls = 0;
    std::unique_ptr<LibusbApi> usb(const DiscoveryItem &) override { ++calls; throw std::logic_error("USB provider invoked"); }
    std::unique_ptr<SpiIo> spi(const DiscoveryItem &) override { ++calls; throw std::logic_error("SPI provider invoked"); }
  } io;
  InjectedTransportFactory factory(io,clock);
  const ReceiveEvent empty=[] { return std::optional<MessageBytes>{}; };
  RealtimeSource source({empty,empty,empty,empty});
  unsigned publications=0;
  RealtimeSink sink([&](OutputTopic,const MessageBytes &) { ++publications; return true; });
  RuntimeIntegration owner(SessionProfile{},clock,discovery,factory,source,sink);
  const auto cycle=owner.cycle();
  if (cycle.callback_failed || discovery.calls || io.calls || publications!=3 || owner.sessions().group_available || owner.snapshot().real_tx_authorized) return 1;
  owner.request_shutdown();
  if (!owner.cycle().shutdown) return 1;
  std::cout << "PASS p8_actual_main_unbound_owner_no_io\n"
            << "{\"suite\":\"p8_actual_main\",\"passed\":1,\"failed\":0,\"skipped\":0,\"real_device_io\":false}\n";
  return 0;
}
}  // namespace openpilot::pandad::compatibility
