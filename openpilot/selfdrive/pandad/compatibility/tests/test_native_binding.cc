#include "../native_binding.h"
#include <algorithm>
#include <functional>
#include <iostream>
#include <stdexcept>

using namespace openpilot::pandad::compatibility;
namespace {
unsigned assertions=0;
#define CHECK(x) do { ++assertions; if (!(x)) throw std::runtime_error(std::string(__func__)+": " #x); } while(false)
struct FakeClock : Clock { std::uint64_t value=1000000; std::uint64_t now_us() override{return value;} void wait_us(std::uint32_t n) override{value+=n;} };
NativeUsbObservation raw(unsigned n, unsigned type=6) {
  NativeUsbObservation r;
  r.serial="offline-test-"+std::to_string(n); r.uid.fill(n); r.board_type=type;
  r.protocol={0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75}; r.application_signature.fill(n+10);
  if (type==6) r.f4_capabilities={'F','4','C','1',1,0,16,0,31,0,0,0,7,0,0,0};
  return r;
}
NativeBindingProfile profile() {
  NativeBindingProfile p; p.binding_status="CANDIDATE";
  for(unsigned i=0;i<2;++i) {
    auto &b=p.slots[i]; b.slot=i; b.expected=raw(i+1); b.transport="USB"; b.exact_mcu="STM32F413VGT6";
    b.physical_evidence_sha256=std::string(64,'a'+i);
    b.capabilities.diagnostic=b.capabilities.harness=b.capabilities.ignition=b.capabilities.fan=b.capabilities.power_monitor=true;
  }
  p.roles.vehicle_can=p.roles.diagnostic=p.roles.harness=p.roles.fan_control=p.roles.power_monitor=0;
  p.roles.ignition_sources={0}; p.roles.ir_control=kRoleUnsupported;
  return p;
}
std::vector<NativeObservedBoard> inventory(const NativeBindingProfile &p) {
  std::vector<NativeObservedBoard> out;
  for(unsigned i=0;i<2;++i) {
    NativeObservedBoard o; o.raw=p.slots[i].expected; o.raw.lease_nonce=i+1; o.transport=p.slots[i].transport;
    o.usb_alt0_verified=o.transport=="USB"; o.spi_first_request_id=o.transport=="SPI" ? i+100 : 0;
    o.spi_selector=p.slots[i].spi_selector;
    out.push_back(o);
  }
  return out;
}
NativeEvidenceVerifier evidence() { return [](const auto &){return true;}; } // explicitly synthetic, no files or hardware
void unbound(const NativeBindingResult &r) {
  CHECK(!r.bound); CHECK(r.profile.binding_status=="UNBOUND");
  CHECK(r.profile.slots[0].expected.identity.empty()); CHECK(r.profile.slots[1].expected.identity.empty());
  CHECK(!r.profile.device_access_enabled && !r.profile.can_tx_enabled && !r.profile.firmware_install_enabled);
}
struct Calls { unsigned transacts=0,cancels=0; };
class PermissiveFake : public Backend {
  std::shared_ptr<Calls> calls_;
public:
  explicit PermissiveFake(std::shared_ptr<Calls> c):calls_(std::move(c)){}
  Result transact(const Request &r,std::uint32_t) override { ++calls_->transacts; return {Status::Ok,r.read_length,std::vector<std::uint8_t>(r.read_length,1),true,false}; }
  void cancel() noexcept override {++calls_->cancels;}
};
std::vector<NativeLease> leases(const NativeBindingProfile &p,FakeClock &clock,std::shared_ptr<Calls> calls=std::make_shared<Calls>()) {
  std::vector<NativeLease> out;
  for(const auto &o:inventory(p)) {
    NativeLease l; l.observed=o; l.observed_us=clock.value; l.io=std::make_unique<OwnedTransport>();
    l.io->backend=std::make_unique<PermissiveFake>(calls); l.io->transport=std::make_unique<Transport>(*l.io->backend,clock);
    out.push_back(std::move(l));
  }
  return out;
}
void mixed(NativeBindingProfile &p) {
  auto &b=p.slots[1]; b.expected=raw(2,7); b.expected.serial.clear(); b.transport="SPI"; b.exact_mcu="STM32H725IGK6";
  b.spi_selector.path="/dev/spidev0.0"; b.spi_selector.identity_evidence_sha256=b.physical_evidence_sha256;
  b.spi_selector.physical_identity_verified=true; b.spi_selector.expected_major=153; b.spi_selector.expected_minor=0;
  b.spi_selector.expected_mode=0; b.spi_selector.expected_bits_per_word=8; b.spi_selector.expected_max_speed_hz=50000000;
}
}
int main() {
  unsigned passed=0,failed=0;
  const auto test=[&](const char *id,const std::function<void()> &fn) {
    try {fn();++passed;std::cout<<"PASS "<<id<<'\n';} catch(const std::exception &e){++failed;std::cerr<<id<<": "<<e.what()<<'\n';}
  };
  test("p9_binding_default_unbound",[]{ unsigned calls=0; NativeBindingProfile p; auto r=validate_native_binding(p,{},[&](const auto &){++calls;return true;}); unbound(r);CHECK(calls==0);CHECK(r.reason=="UNBOUND_PROFILE"); });
  test("p9_binding_partial_unbound_rejected",[]{ NativeBindingProfile p;p.slots[0].exact_mcu="STM32F413VGT6";auto r=validate_native_binding(p,{});unbound(r);CHECK(r.reason=="PARTIAL_UNBOUND_PROFILE");p=NativeBindingProfile{};p.roles.vehicle_can=0;r=validate_native_binding(p,{});CHECK(r.reason=="PARTIAL_UNBOUND_ROLES"); });
  test("p9_binding_no_permission_from_profile",[]{for(unsigned i=0;i<3;++i){auto p=profile();if(i==0)p.device_access_enabled=true;if(i==1)p.can_tx_enabled=true;if(i==2)p.firmware_install_enabled=true;unbound(validate_native_binding(p,inventory(p),evidence()));} });
  test("p9_binding_physical_verifier_required",[]{ auto p=profile();unbound(validate_native_binding(p,inventory(p)));unbound(validate_native_binding(p,inventory(p),[](const auto &){return false;}));unbound(validate_native_binding(p,inventory(p),[](const auto &)->bool{throw std::runtime_error("bad evidence");})); });
  test("p9_binding_complete_dual_usb_fixed_slots",[]{auto p=profile();auto obs=inventory(p);std::reverse(obs.begin(),obs.end());auto r=validate_native_binding(p,obs,evidence());CHECK(r.bound);CHECK(r.profile.binding_status=="NATIVE_READ_ONLY_BOUND");CHECK(r.observations[0].raw.uid==p.slots[0].expected.uid);CHECK(r.profile.slots[0].expected.identity.rfind("uid:",0)==0);CHECK(r.profile.slots[0].expected.application_signature.size()==256);CHECK(!r.profile.can_tx_enabled);SessionRegistry registry(r.profile);CHECK(!registry.reconcile({}).group_available);});
  test("p9_binding_missing_extra_duplicate_board",[]{auto p=profile();auto obs=inventory(p);obs.pop_back();unbound(validate_native_binding(p,obs,evidence()));obs=inventory(p);obs.push_back(obs.front());unbound(validate_native_binding(p,obs,evidence()));obs=inventory(p);obs[1].raw.uid=obs[0].raw.uid;unbound(validate_native_binding(p,obs,evidence()));p.slots[1].expected.uid=p.slots[0].expected.uid;unbound(validate_native_binding(p,inventory(p),evidence()));});
  test("p9_binding_duplicate_serial_and_nonce",[]{auto p=profile();auto obs=inventory(p);obs[1].raw.serial=obs[0].raw.serial;unbound(validate_native_binding(p,obs,evidence()));obs=inventory(p);obs[1].raw.lease_nonce=obs[0].raw.lease_nonce;unbound(validate_native_binding(p,obs,evidence()));obs[1].raw.lease_nonce=0;unbound(validate_native_binding(p,obs,evidence()));});
  test("p9_binding_replacement_and_protocol_mismatch",[]{auto p=profile();for(unsigned n=0;n<5;++n){auto obs=inventory(p);if(n==0)obs[0].raw.uid[0]++;if(n==1)obs[0].raw.application_signature[90]++;if(n==2)obs[0].raw.protocol[0]++;if(n==3)obs[0].raw.board_type=7;if(n==4)obs[0].raw.f4_capabilities.clear();unbound(validate_native_binding(p,obs,evidence()));}});
  test("p9_binding_expected_identity_validation",[]{for(unsigned n=0;n<7;++n){auto p=profile();if(n==0)p.slots[0].expected.uid.fill(0);if(n==1)p.slots[0].expected.application_signature.fill(255);if(n==2)p.slots[0].expected.protocol[2]++;if(n==3)p.slots[0].expected.lease_nonce=1;if(n==4)p.slots[0].expected.serial="bad serial";if(n==5)p.slots[0].expected.f4_capabilities.clear();if(n==6)p.slots[0].slot=1;unbound(validate_native_binding(p,inventory(p),evidence()));}});
  test("p9_binding_no_descriptor_mcu_inference",[]{for(unsigned n=0;n<4;++n){auto p=profile();if(n==0)p.slots[0].exact_mcu.clear();if(n==1)p.slots[0].exact_mcu="STM32H725IGK6";if(n==2)p.slots[0].physical_evidence_sha256.clear();if(n==3)p.slots[0].physical_evidence_sha256=std::string(64,'0');unbound(validate_native_binding(p,inventory(p),evidence()));}});
  test("p9_binding_no_alt_descriptor_inference",[]{auto p=profile();auto obs=inventory(p);obs[0].usb_alt0_verified=false;unbound(validate_native_binding(p,obs,evidence()));obs=inventory(p);obs[0].spi_first_request_id=1;unbound(validate_native_binding(p,obs,evidence()));});
  test("p9_binding_f4_capabilities_and_roles",[]{for(unsigned n=0;n<6;++n){auto p=profile();if(n==0)p.slots[0].capabilities.can_fd=true;if(n==1)p.slots[0].capabilities.ir=true;if(n==2)p.roles.harness=3;if(n==3)p.roles.ignition_sources.clear();if(n==4)p.roles.fan_control=kRoleUnsupported;if(n==5)p.slots[0].capabilities.harness=false;unbound(validate_native_binding(p,inventory(p),evidence()));}});
  test("p9_binding_mixed_usb_h7_spi",[]{auto p=profile();mixed(p);auto r=validate_native_binding(p,inventory(p),evidence());CHECK(r.bound);CHECK(r.profile.slots[1].expected.transport=="SPI");CHECK(r.profile.slots[1].expected.mcu_family=="H7");});
  test("p9_binding_spi_selector_and_session_evidence",[]{for(unsigned n=0;n<8;++n){auto p=profile();mixed(p);auto &s=p.slots[1].spi_selector;if(n==0)s.expected_mode.reset();if(n==1)s.expected_mode=1;if(n==2)s.expected_bits_per_word=16;if(n==3)s.expected_max_speed_hz=60000000;if(n==4)s.path="/dev/spidev0.1/../0.0";if(n==5)s.identity_evidence_sha256=std::string(64,'c');auto obs=inventory(p);if(n==6)obs[1].spi_first_request_id=0;if(n==7)obs[1].usb_alt0_verified=true;unbound(validate_native_binding(p,obs,evidence()));}});
  test("p9_binding_f4_spi_not_in_bsp",[]{auto p=profile();mixed(p);p.slots[1].expected=raw(2);p.slots[1].expected.serial.clear();p.slots[1].exact_mcu="STM32F413VGT6";unbound(validate_native_binding(p,inventory(p),evidence()));});
  test("p9_binding_native_not_fixture_disguise",[]{auto p=profile();auto r=validate_native_binding(p,inventory(p),evidence());CHECK(r.bound);auto s=r.profile;s.purpose="offline_fixture";s.schema=kSessionProfileSchema;bool threw=false;try{SessionRegistry registry(s);}catch(...){threw=true;}CHECK(threw);});
  test("p9_binding_usb_helper_default_denied",[]{NativeUsbInventory usb;FakeClock clock;NativeBindingResult empty;CHECK(!native_usb_read_only_lease(usb,0,empty,0,clock).io);auto p=profile();auto r=validate_native_binding(p,inventory(p),evidence());CHECK(!native_usb_read_only_lease(usb,0,r,0,clock).io);CHECK(!native_usb_read_only_lease(usb,0,r,2,clock,true).io);});
  test("p9_binding_spi_helper_rejects_mismatch",[]{NativeSpiProbeResult probe;NativeObservedBoard observed;CHECK(!native_spi_read_only_lease(std::move(probe),observed).io);});
  test("p9_binding_provider_default_no_discovery",[]{FakeClock clock;auto p=profile();NativeReadOnlyProviders provider(p,evidence(),clock);CHECK(provider.snapshot().items.empty());CHECK(!provider.stage(leases(p,clock),1,clock.value));CHECK(provider.snapshot().items.empty());unbound(provider.binding());});
  test("p9_binding_provider_single_handoff_and_cancel",[]{FakeClock clock;auto p=profile();auto calls=std::make_shared<Calls>();NativeReadOnlyProviders provider(p,evidence(),clock);CHECK(provider.stage(leases(p,clock,calls),1,clock.value,true));auto snapshot=provider.snapshot();auto bound=provider.binding();auto owned=provider.open(snapshot.items[0],bound.profile.slots[0]);CHECK(owned!=nullptr);CHECK(!provider.open(snapshot.items[0],bound.profile.slots[0]));CHECK(calls->transacts==0);owned.reset();CHECK(calls->cancels>0);});
  test("p9_binding_provider_blocks_write_and_mutating_in",[]{FakeClock clock;auto p=profile();auto calls=std::make_shared<Calls>();NativeReadOnlyProviders provider(p,evidence(),clock);CHECK(provider.stage(leases(p,clock,calls),1,clock.value,true));auto owned=provider.open(provider.snapshot().items[0],provider.binding().profile.slots[0]);Request r;r.kind=Kind::ControlWrite;r.request=0xdc;r.value=19;CHECK(owned->backend->transact(r,1).status==Status::Invalid);r.kind=Kind::BulkWrite;r.endpoint=3;r.payload={1};CHECK(owned->backend->transact(r,1).status==Status::Invalid);r=Request{};r.kind=Kind::ControlRead;r.request=0xd8;r.read_length=1;CHECK(owned->backend->transact(r,1).status==Status::Invalid);CHECK(calls->transacts==0);r.request=0xc3;r.read_length=12;CHECK(owned->backend->transact(r,1).status==Status::Ok);CHECK(calls->transacts==1);});
  test("p9_binding_provider_stale_and_future_clock",[]{for(unsigned n=0;n<3;++n){FakeClock clock;auto p=profile();NativeReadOnlyProviders provider(p,evidence(),clock);auto stamp=n==0 ? 0 : n==1 ? clock.value+1 : clock.value-kLoopMaxGapUs;CHECK(!provider.stage(leases(p,clock),1,stamp,true));CHECK(provider.snapshot().items.empty());}});
  test("p9_binding_provider_rejects_retimestamped_probe",[]{FakeClock clock;auto p=profile();NativeReadOnlyProviders provider(p,evidence(),clock);auto ls=leases(p,clock);clock.value+=1;CHECK(!provider.stage(std::move(ls),1,clock.value,true));ls=leases(p,clock);ls[0].observed_us-=1;CHECK(!provider.stage(std::move(ls),2,clock.value,true));});
  test("p9_binding_spi_observed_selector_exact",[]{auto p=profile();mixed(p);auto obs=inventory(p);obs[1].spi_selector.path="/dev/spidev0.1";unbound(validate_native_binding(p,obs,evidence()));});
  test("p9_binding_provider_does_not_refresh_timestamp",[]{FakeClock clock;auto p=profile();NativeReadOnlyProviders provider(p,evidence(),clock);CHECK(provider.stage(leases(p,clock),1,clock.value,true));auto snap=provider.snapshot();clock.value+=kLoopMaxGapUs;CHECK(provider.snapshot().observed_us==snap.observed_us);CHECK(!provider.open(snap.items[0],provider.binding().profile.slots[0]));});
  test("p9_binding_provider_consumed_lease_not_reused",[]{FakeClock clock;auto p=profile();NativeReadOnlyProviders provider(p,evidence(),clock);CHECK(provider.stage(leases(p,clock),1,clock.value,true));CHECK(!provider.stage(leases(p,clock),2,clock.value,true));CHECK(provider.snapshot().items.empty());auto fresh=leases(p,clock);for(auto &l:fresh)l.observed.raw.lease_nonce+=2;CHECK(provider.stage(std::move(fresh),3,clock.value,true));});
  test("p9_binding_provider_exact_item_match",[]{FakeClock clock;auto p=profile();NativeReadOnlyProviders provider(p,evidence(),clock);CHECK(provider.stage(leases(p,clock),1,clock.value,true));auto item=provider.snapshot().items[0];auto b=provider.binding().profile.slots[0];item.handle_nonce="old";CHECK(!provider.open(item,b));item=provider.snapshot().items[0];item.usb_alt0_verified=false;CHECK(!provider.open(item,b));b.safety_config_semantics="allOutput";CHECK(!provider.open(provider.snapshot().items[0],b));});
  test("p9_binding_provider_partial_failure_closes_group",[]{FakeClock clock;auto p=profile();NativeReadOnlyProviders provider(p,evidence(),clock);auto ls=leases(p,clock);ls[1].observed.raw.uid[0]++;CHECK(!provider.stage(std::move(ls),1,clock.value,true));CHECK(provider.snapshot().items.empty());unbound(provider.binding());});
  test("p9_binding_unbound_rejects_partial_timestamp",[]{NativeBindingProfile p;p.slots[0].expected.observed_us=123;auto r=validate_native_binding(p,{});unbound(r);CHECK(r.reason=="PARTIAL_UNBOUND_PROFILE");});
  test("p9_binding_spi_helper_preserves_actual_probe_metadata",[]{FakeClock clock;auto p=profile();mixed(p);auto o=inventory(p)[1];for(unsigned n=0;n<4;++n){NativeSpiProbeResult probe;probe.status=Status::Ok;probe.observation=o.raw;probe.validated_selector=o.spi_selector;probe.first_request_id=o.spi_first_request_id;probe.started_us=clock.value-50;probe.completed_us=clock.value;auto fake=leases(p,clock);probe.transport=std::move(fake[1].io);auto requested=o;if(n==1)requested.spi_first_request_id++;if(n==2)requested.spi_selector.path="/dev/spidev0.1";if(n==3)requested.raw.lease_nonce++;auto result=native_spi_read_only_lease(std::move(probe),requested);CHECK(static_cast<bool>(result.io)==(n==0));CHECK(result.observed_us==(n==0 ? clock.value : 0));}});
  test("p9_binding_handoff_rejects_bad_request_shape_budget",[]{FakeClock clock;auto p=profile();auto calls=std::make_shared<Calls>();NativeReadOnlyProviders provider(p,evidence(),clock);CHECK(provider.stage(leases(p,clock,calls),1,clock.value,true));auto owned=provider.open(provider.snapshot().items[0],provider.binding().profile.slots[0]);Request r;r.kind=Kind::ControlRead;r.request=0xc3;r.read_length=12;CHECK(owned->backend->transact(r,0).status==Status::Invalid);CHECK(owned->backend->transact(r,kBackendSliceMs+1).status==Status::Invalid);r.payload={1};CHECK(owned->backend->transact(r,1).status==Status::Invalid);CHECK(calls->transacts==0);});
  test("p9_binding_runtime_owner_read_only_gate",[]{FakeClock clock;auto p=profile();auto calls=std::make_shared<Calls>();NativeReadOnlyProviders provider(p,evidence(),clock);CHECK(provider.stage(leases(p,clock,calls),1,clock.value,true));const ReceiveEvent empty=[]{return std::optional<MessageBytes>{};};RealtimeSource source({empty,empty,empty,empty});RealtimeSink sink([](OutputTopic,const MessageBytes &){return true;});RuntimeIntegration owner(provider.binding().profile,clock,provider,provider,source,sink);owner.refresh();CHECK(!owner.snapshot().real_tx_authorized);owner.request_shutdown();CHECK(owner.cycle().shutdown);CHECK(!owner.sessions().group_available);});
  std::cout<<"{\"suite\":\"p9_native_binding\",\"passed\":"<<passed<<",\"failed\":"<<failed<<",\"skipped\":0,\"assertions\":"<<assertions<<",\"real_device_io\":false}\n";
  return failed ? 1:0;
}
