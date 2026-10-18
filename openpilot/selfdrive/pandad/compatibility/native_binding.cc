#include "native_binding.h"
#include <algorithm>

namespace openpilot::pandad::compatibility {
namespace {
template <class T> std::string hex(const T &bytes) {
  constexpr char chars[] = "0123456789abcdef";
  std::string out;
  for (auto byte : bytes) { out += chars[byte >> 4]; out += chars[byte & 15]; }
  return out;
}
bool digest(const std::string &s) {
  return s.size() == 64 && std::any_of(s.begin(),s.end(),[](char c){return c != '0';}) &&
    std::all_of(s.begin(),s.end(),[](char c){return (c>='0' && c<='9') || (c>='a' && c<='f');});
}
bool printable(const std::string &s) {
  return !s.empty() && s.size() <= 128 && std::all_of(s.begin(),s.end(),[](unsigned char c){return c>32 && c<127;});
}
bool same_raw(NativeUsbObservation a, NativeUsbObservation b) {
  a.lease_nonce = b.lease_nonce = 0;
  return a == b;
}
bool raw_valid(const NativeUsbObservation &r, const std::string &transport) {
  const std::array<std::uint8_t,8> protocol{{0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75}};
  const std::vector<std::uint8_t> f4{'F','4','C','1',1,0,16,0,31,0,0,0,7,0,0,0};
  const auto nonblank=[](const auto &a) {
    return !std::all_of(a.begin(),a.end(),[](auto b){return b==0;}) && !std::all_of(a.begin(),a.end(),[](auto b){return b==255;});
  };
  return (transport == "USB" ? printable(r.serial) : transport == "SPI" && r.serial.empty()) &&
    nonblank(r.uid) && nonblank(r.application_signature) && r.protocol == protocol &&
    ((r.board_type == 6 && r.f4_capabilities == f4) || (r.board_type == 7 && r.f4_capabilities.empty()));
}
std::string nonce(const NativeObservedBoard &o) { return o.transport + ":" + std::to_string(o.raw.lease_nonce); }
class ReadOnlyHandoff final : public Backend {
public:
  explicit ReadOnlyHandoff(std::unique_ptr<Backend> inner) : inner_(std::move(inner)) {}
  Result transact(const Request &r, std::uint32_t budget) override {
    if (!valid_request(r) || !budget || budget>kBackendSliceMs) return {Status::Invalid,0,{},true,false};
    bool allowed=r.kind==Kind::BulkRead && r.endpoint==0x81;
    if (r.kind==Kind::ControlRead && r.payload.empty()) {
      const auto length=r.request==0xc1 ? 1U : r.request==0xc3 ? 12U : r.request==0xdd ? 8U :
        (r.request==0xd3 || r.request==0xd4 || r.request==0xc2) ? 64U : r.request==0xd5 ? 16U : r.request==0xd2 ? 48U : r.request==0xb2 ? 2U : 0U;
      allowed=(length && !r.index && r.read_length==length && (r.request==0xc2 ? r.value<3 : r.value==0));
    }
    if (!allowed || cancelled_) return {cancelled_ ? Status::Cancelled : Status::Invalid,0,{},true,false};
    return inner_->transact(r,budget);
  }
  void cancel() noexcept override { cancelled_=true; inner_->cancel(); }
private:
  std::unique_ptr<Backend> inner_;
  std::atomic<bool> cancelled_{false};
};
SessionIdentity identity(const NativeBoardBinding &b) {
  return {"uid:"+hex(b.expected.uid), b.expected.board_type==6 ? "DOS":"RED_PANDA",
    b.expected.board_type==6 ? "F4":"H7",b.exact_mcu,b.transport,kSessionProtocol,b.capabilities,hex(b.expected.application_signature)};
}
}  // namespace

NativeBindingResult validate_native_binding(const NativeBindingProfile &p, const std::vector<NativeObservedBoard> &inventory,
                                            const NativeEvidenceVerifier &verify) {
  NativeBindingResult result;
  result.profile.schema = kNativeSessionProfileSchema;
  result.profile.purpose = "native_read_only_candidate";
  const auto reject=[&](const char *reason) { result.reason=reason; return result; };
  if (p.schema != "songplus-native-binding/v1" || p.device_access_enabled || p.can_tx_enabled || p.firmware_install_enabled)
    return reject("INVALID_SCHEMA_OR_PERMISSION");
  if (p.binding_status == "UNBOUND") {
    const SessionCapabilities empty_caps;
    for (std::size_t i=0;i<2;++i) {
      const auto &b=p.slots[i];
      if (b.slot!=i || !(b.expected==NativeUsbObservation{}) || b.expected.observed_us || !b.transport.empty() || !b.exact_mcu.empty() ||
          !(b.capabilities==empty_caps) || !b.physical_evidence_sha256.empty() || !b.spi_selector.path.empty() ||
          !b.spi_selector.identity_evidence_sha256.empty() || b.spi_selector.physical_identity_verified ||
          b.spi_selector.expected_major || b.spi_selector.expected_minor || b.spi_selector.expected_mode ||
          b.spi_selector.expected_bits_per_word || b.spi_selector.expected_max_speed_hz) return reject("PARTIAL_UNBOUND_PROFILE");
    }
    SessionProfile empty; empty.roles=p.roles;
    try { const SessionRegistry checked(empty); (void)checked; }
    catch (...) { return reject("PARTIAL_UNBOUND_ROLES"); }
    return result;
  }
  if (p.binding_status != "CANDIDATE" || !verify) return reject("PHYSICAL_EVIDENCE_VERIFIER_REQUIRED");
  if (inventory.size() != 2) return reject("COMPLETE_TWO_BOARD_INVENTORY_REQUIRED");
  if (!inventory[0].raw.lease_nonce || !inventory[1].raw.lease_nonce ||
      inventory[0].raw.lease_nonce == inventory[1].raw.lease_nonce || inventory[0].raw.uid == inventory[1].raw.uid)
    return reject("DUPLICATE_OR_INVALID_LEASE_IDENTITY");
  if (p.slots[0].expected.uid == p.slots[1].expected.uid) return reject("DUPLICATE_BOUND_UID");
  if (inventory[0].transport == "USB" && inventory[1].transport == "USB" && inventory[0].raw.serial == inventory[1].raw.serial)
    return reject("DUPLICATE_USB_SERIAL");
  SessionProfile candidate = result.profile;
  candidate.binding_status = "NATIVE_READ_ONLY_BOUND";
  candidate.roles = p.roles;
  for (std::size_t slot=0;slot<2;++slot) {
    const auto &b=p.slots[slot];
    if (b.slot != slot || b.expected.lease_nonce || b.expected.observed_us || !raw_valid(b.expected,b.transport)) return reject("INVALID_EXPECTED_IDENTITY");
    if (!digest(b.physical_evidence_sha256) || !printable(b.exact_mcu) ||
        (b.expected.board_type==6 ? b.exact_mcu.rfind("STM32F413",0)!=0 : b.exact_mcu.rfind("STM32H7",0)!=0))
      return reject("EXACT_MCU_OR_PHYSICAL_EVIDENCE_MISSING");
    if (b.expected.board_type==6 && (b.capabilities.can_fd || b.capabilities.ir || b.transport=="SPI"))
      return reject("F4_UNSUPPORTED_CAPABILITY_OR_TRANSPORT");
    if (b.transport=="SPI" && (!valid_native_spi_selector(b.spi_selector) || *b.spi_selector.expected_mode!=0 ||
        *b.spi_selector.expected_bits_per_word!=8 || *b.spi_selector.expected_max_speed_hz>50000000 || b.spi_selector.identity_evidence_sha256 != b.physical_evidence_sha256))
      return reject("SPI_NODE_CONFIGURATION_EVIDENCE_MISSING");
    try { if (!verify(b)) return reject("PHYSICAL_EVIDENCE_NOT_VERIFIED"); }
    catch (...) { return reject("PHYSICAL_EVIDENCE_VERIFIER_FAILED"); }
    const auto found=std::find_if(inventory.begin(),inventory.end(),[&](const auto &o){return o.raw.uid==b.expected.uid;});
    if (found==inventory.end() || found->transport!=b.transport || !same_raw(found->raw,b.expected)) return reject("OBSERVED_IDENTITY_MISMATCH");
    if ((b.transport=="USB" && (!found->usb_alt0_verified || found->spi_first_request_id)) ||
        (b.transport=="SPI" && (!found->spi_first_request_id || found->usb_alt0_verified || !(found->spi_selector==b.spi_selector)))) return reject("SESSION_ACTIVATION_EVIDENCE_MISSING");
    result.observations[slot]=*found;
    candidate.slots[slot]={static_cast<std::uint32_t>(slot),identity(b),"noOutput"};
  }
  try { const SessionRegistry checked(candidate); (void)checked; }
  catch (...) { return reject("INVALID_ROLES_OR_CAPABILITIES"); }
  result.profile=std::move(candidate); result.bound=true; result.reason="MATCHED_READ_ONLY_CANDIDATE_NO_DEPLOYMENT_PERMISSION";
  return result;
}
NativeLease native_usb_read_only_lease(NativeUsbInventory &inventory, std::size_t index, const NativeBindingResult &binding, std::size_t slot,
                                       Clock &clock, bool allow) {
  NativeLease result;
  if (!binding.bound || slot>=2) return result;
  const auto &observed=binding.observations[slot]; result.observed=observed;
  if (!allow || observed.transport!="USB" || !observed.usb_alt0_verified || observed.spi_first_request_id) return result;
  NativeUsbActivation activation;
  activation.lease_nonce=observed.raw.lease_nonce; activation.allow_device_io=true;
  activation.profile_validated=true; activation.bulk_alt0_verified=true;
  std::uint64_t activated_us=0;
  auto backend=inventory.activate(index,observed.raw,activation,clock,&activated_us);
  if (!backend) return result;
  result.observed_us=activated_us;
  result.io=std::make_unique<OwnedTransport>(); result.io->backend=std::move(backend);
  result.io->transport=std::make_unique<Transport>(*result.io->backend,clock);
  return result;
}
NativeLease native_spi_read_only_lease(NativeSpiProbeResult &&probe, const NativeObservedBoard &observed) {
  NativeLease result; result.observed=observed;
  if (probe.complete() && observed.transport=="SPI" && observed.spi_first_request_id && !observed.usb_alt0_verified &&
      probe.observation == observed.raw && probe.first_request_id==observed.spi_first_request_id &&
      probe.validated_selector==observed.spi_selector) {
    result.observed_us=probe.completed_us; result.io=std::move(probe.transport);
  }
  return result;
}
bool NativeReadOnlyProviders::stage(std::vector<NativeLease> leases, std::uint64_t revision, std::uint64_t observed_us, bool allow) {
  leases_={}; snapshot_={}; binding_={};
  const auto now=clock_.now_us();
  if (!allow || !revision || revision<=last_revision_ || !observed_us || observed_us>now || now-observed_us>=kLoopMaxGapUs || leases.size()!=2)
    return false;
  std::vector<NativeObservedBoard> observations;
  std::uint64_t oldest=now;
  for (const auto &lease:leases) {
    if (!lease.io || !lease.io->backend || !lease.io->transport || !lease.io->transport->healthy() ||
        !lease.observed_us || lease.observed_us>now || now-lease.observed_us>=kLoopMaxGapUs ||
        !lease.observed.raw.lease_nonce || consumed_.count(lease.observed.raw.lease_nonce) || consumed_.size()>126) return false;
    oldest=std::min(oldest,lease.observed_us);
    observations.push_back(lease.observed);
  }
  if (observed_us!=oldest) return false;
  last_revision_=revision;
  for (const auto &o:observations) consumed_.insert(o.raw.lease_nonce);
  binding_=validate_native_binding(profile_,observations,verifier_);
  if (!binding_.bound) return false;
  snapshot_.revision=revision; snapshot_.observed_us=observed_us;
  for (std::size_t slot=0;slot<2;++slot) {
    const auto &o=binding_.observations[slot];
    const auto it=std::find_if(leases.begin(),leases.end(),[&](const auto &l){return l.observed.raw.lease_nonce==o.raw.lease_nonce;});
    leases_[slot]=std::move(it->io);
    // Enforce receive-only at the final handoff as well, even if a caller
    // accidentally supplies a provider created with a broader grant.
    leases_[slot]->transport.reset();
    leases_[slot]->backend=std::make_unique<ReadOnlyHandoff>(std::move(leases_[slot]->backend));
    leases_[slot]->transport=std::make_unique<Transport>(*leases_[slot]->backend,clock_);
    snapshot_.items.push_back({binding_.profile.slots[slot].expected,nonce(o),o.usb_alt0_verified,o.spi_first_request_id});
  }
  return true;
}
std::unique_ptr<OwnedTransport> NativeReadOnlyProviders::open(const DiscoveryItem &item, const SessionBinding &binding) {
  if (!binding_.bound || binding.slot>=2 || binding.safety_config_semantics!="noOutput") return nullptr;
  const auto slot=binding.slot;
  const auto &expected=snapshot_.items[slot];
  const auto now=clock_.now_us();
  if (now<snapshot_.observed_us || now-snapshot_.observed_us>=kLoopMaxGapUs ||
      !(item.identity==binding.expected) || !(item.identity==expected.identity) || item.handle_nonce!=expected.handle_nonce ||
      item.usb_alt0_verified!=expected.usb_alt0_verified || item.spi_first_request_id!=expected.spi_first_request_id) return nullptr;
  return std::move(leases_[slot]);
}
}  // namespace openpilot::pandad::compatibility
