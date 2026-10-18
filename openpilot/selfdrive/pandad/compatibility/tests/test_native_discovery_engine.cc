#include "../native_discovery_executor.h"
#include "native_discovery_spi_fixture.h"
#include <algorithm>
#include <functional>
#include <iostream>
#include <map>
#include <stdexcept>

using namespace openpilot::pandad::compatibility;
namespace {
unsigned assertions=0;
#define CHECK(x) do{++assertions;if(!(x))throw std::runtime_error(std::string(__func__)+":"+std::to_string(__LINE__)+": " #x);}while(false)
struct FakeClock final:Clock {std::uint64_t value=1000000;std::uint64_t now_us()override{return value;}void wait_us(std::uint32_t n)override{value+=n;}};
NativeUsbObservation raw(unsigned n){NativeUsbObservation r;r.serial="offline-device-"+std::to_string(n);r.uid.fill(n);r.board_type=6;r.protocol={0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75};r.application_signature.fill(n+10);r.f4_capabilities={'F','4','C','1',1,0,16,0,31,0,0,0,7,0,0,0};return r;}
UsbDeviceInfo descriptor(unsigned n){return{n,0xbbaa,0xddcc,0x600,3,true,{{0,0,{{0x81,2,64},{3,2,64}}}}};}
struct Wire {
  std::shared_ptr<FakeClock> clock=std::make_shared<FakeClock>();
  std::vector<UsbDeviceInfo> devices{descriptor(1),descriptor(2)};
  std::map<UsbHandle,NativeUsbObservation> boards{{1,raw(1)},{2,raw(2)}};
  unsigned factories=0,inits=0,enumerations=0,opens=0,serials=0,claims=0,closes=0,releases=0,shutdowns=0,writes=0;
  std::vector<unsigned> requests;
  std::uint64_t call_cost=1;
  int init_error=0,enumerate_error=0,short_request=-1,fail_request=-1;
};
class FakeUsb final:public LibusbApi {
  std::shared_ptr<Wire> s;
public:
  explicit FakeUsb(std::shared_ptr<Wire> wire):s(std::move(wire)){}
  int initialize()override{++s->inits;return s->init_error;}
  int enumerate(std::vector<UsbDeviceInfo>&out)override{++s->enumerations;out=s->devices;return s->enumerate_error;}
  int open(UsbDeviceId d,UsbHandle&h)override{++s->opens;h=d;return 0;}
  int serial(UsbHandle h,std::uint8_t,std::string&out,std::uint32_t)override{++s->serials;out=s->boards.at(h).serial;return 0;}
  int claim(UsbHandle,int n)override{CHECK(n==0);++s->claims;return 0;}
  int control(UsbHandle h,std::uint8_t type,std::uint8_t request,std::uint16_t value,std::uint16_t index,std::uint8_t *data,std::uint16_t size,std::uint32_t timeout)override{
    s->clock->value+=s->call_cost;CHECK(timeout&&timeout<=5);CHECK(!value&&!index);if(type!=0xc0){++s->writes;return kUsbInvalid;}
    s->requests.push_back(request);if(s->fail_request==request)return kUsbNoDevice;
    const auto &b=s->boards.at(h);std::vector<std::uint8_t> bytes;
    if(request==0xc3)bytes.assign(b.uid.begin(),b.uid.end());else if(request==0xc1)bytes={b.board_type};else if(request==0xdd)bytes.assign(b.protocol.begin(),b.protocol.end());
    else if(request==0xd3)bytes.assign(b.application_signature.begin(),b.application_signature.begin()+64);else if(request==0xd4)bytes.assign(b.application_signature.begin()+64,b.application_signature.end());else if(request==0xd5)bytes=b.f4_capabilities;else return kUsbInvalid;
    CHECK(bytes.size()==size);std::copy(bytes.begin(),bytes.end(),data);return static_cast<int>(bytes.size())-(s->short_request==request?1:0);
  }
  int bulk(UsbHandle,std::uint8_t ep,std::uint8_t*,int,int&actual,std::uint32_t)override{if(ep!=0x81)++s->writes;actual=0;return ep==0x81?0:kUsbInvalid;}
  void release(UsbHandle,int)override{++s->releases;}
  void close(UsbHandle)override{++s->closes;}
  void shutdown()override{++s->shutdowns;}
};
NativeBindingProfile profile(){NativeBindingProfile p;p.binding_status="CANDIDATE";for(unsigned i=0;i<2;++i){auto &b=p.slots[i];b.expected=raw(i+1);b.transport="USB";b.exact_mcu="STM32F413VGT6";b.physical_evidence_sha256=std::string(64,'a'+i);b.capabilities.diagnostic=b.capabilities.harness=b.capabilities.ignition=b.capabilities.fan=b.capabilities.power_monitor=true;}p.roles={0,0,0,{0,1},0,0,kRoleUnsupported};return p;}
NativeDiscoveryEngineConfig config(const std::shared_ptr<Wire>&w){NativeDiscoveryEngineConfig c;c.profile=profile();c.physical_evidence=[](const auto&){return true;};c.usb_session_alt0=[](const auto&){return true;};c.clock=w->clock;c.usb_api=[w]{++w->factories;return std::make_unique<FakeUsb>(w);};c.allow_read_only_device_io=true;return c;}
struct Rig {
  std::shared_ptr<Wire> wire=std::make_shared<Wire>();
  std::unique_ptr<NativeDiscoveryEngine> engine;
  std::atomic<bool> cancelled{false};
  DiscoveryTaskRequest request;
  DiscoveryTaskResult result;
  Rig():engine(make_native_discovery_engine(config(wire))){}
  void first(){request={1,wire->clock->value,wire->clock->value+100000,DiscoveryTaskKind::Discover,{}};result=engine->execute(request,cancelled);CHECK(result.snapshot.items.size()==2);CHECK(result.payload!=nullptr);}
  DiscoveryTaskResult again(){request={request.generation+1,wire->clock->value,wire->clock->value+100000,DiscoveryTaskKind::Reprobe,result.snapshot};return engine->execute(request,cancelled);}
  NativeDiscoveryHandoff &handoff(){auto *h=dynamic_cast<NativeDiscoveryHandoff *>(result.payload.get());CHECK(h!=nullptr);return *h;}
};
struct MixedRig {
  std::shared_ptr<Wire> wire=std::make_shared<Wire>();
  std::shared_ptr<p10_spi_fixture::State> spi=std::make_shared<p10_spi_fixture::State>();
  std::unique_ptr<NativeDiscoveryEngine> engine;
  std::atomic<bool> cancelled{false};
  DiscoveryTaskRequest request;
  DiscoveryTaskResult result;
  MixedRig(){
    wire->devices.resize(1);auto c=config(wire);auto &b=c.profile.slots[1];
    b.expected=p10_spi_fixture::observation();b.transport="SPI";b.exact_mcu="STM32H725IGK6";b.spi_selector=p10_spi_fixture::selector();
    c.spi_cooperative_ownership=[](const auto&){return true;};c.spi_syscalls=[state=spi]{return p10_spi_fixture::calls(state);};
    spi->on_message=[w=wire]{++w->clock->value;};engine=make_native_discovery_engine(c);
  }
  void first(){request={request.generation+1,wire->clock->value,wire->clock->value+100000,DiscoveryTaskKind::Discover,{}};result=engine->execute(request,cancelled);CHECK(result.snapshot.items.size()==2);}
  DiscoveryTaskResult again(){request={request.generation+1,wire->clock->value,wire->clock->value+100000,DiscoveryTaskKind::Reprobe,result.snapshot};return engine->execute(request,cancelled);}
  NativeDiscoveryHandoff &handoff(){auto *h=dynamic_cast<NativeDiscoveryHandoff *>(result.payload.get());CHECK(h);return *h;}
};
}
int main(){unsigned passed=0,failed=0;const auto test=[&](const char *id,const std::function<void()>&fn){try{fn();++passed;std::cout<<"PASS "<<id<<'\n';}catch(const std::exception&e){++failed;std::cerr<<id<<": "<<e.what()<<'\n';}};
  test("p10_engine_default_unbound_never_constructs_native_api",[]{auto w=std::make_shared<Wire>();auto c=config(w);c.profile={};auto engine=make_native_discovery_engine(c);std::atomic<bool> cancel{false};DiscoveryTaskRequest r{1,w->clock->value,w->clock->value+100000,DiscoveryTaskKind::Discover,{}};CHECK(engine->execute(r,cancel).snapshot.items.empty());CHECK(w->factories==0&&w->opens==0);});
  test("p10_engine_permission_and_verifier_default_denied",[]{for(unsigned n=0;n<5;++n){auto w=std::make_shared<Wire>();auto c=config(w);if(n==0)c.allow_read_only_device_io=false;if(n==1)c.physical_evidence={};if(n==2)c.physical_evidence=[](const auto&){return false;};if(n==3)c.usb_session_alt0={};if(n==4)c.profile.slots[0].exact_mcu.clear();auto engine=make_native_discovery_engine(c);std::atomic<bool> cancel{false};DiscoveryTaskRequest r{1,w->clock->value,w->clock->value+100000,DiscoveryTaskKind::Discover,{}};CHECK(engine->execute(r,cancel).snapshot.items.empty());CHECK(w->factories==0);}});
  test("p10_engine_dual_usb_p9_discovery_and_claim_once",[]{Rig r;r.first();CHECK(r.wire->opens==2&&r.wire->claims==2&&r.wire->writes==0);CHECK(r.handoff().profile.binding_status=="NATIVE_READ_ONLY_BOUND");CHECK(r.handoff().ports[0]->active()&&r.handoff().ports[1]->active());CHECK(r.result.snapshot.observed_us==std::min(r.result.slot_observed_us[0],r.result.slot_observed_us[1]));CHECK(r.result.slot_observed_us[0]<r.result.slot_observed_us[1]);});
  test("p10_engine_shared_f4_firmware_signature_deduplicated",[]{auto w=std::make_shared<Wire>();w->boards[2].application_signature=w->boards[1].application_signature;auto c=config(w);c.profile.slots[1].expected.application_signature=c.profile.slots[0].expected.application_signature;auto e=make_native_discovery_engine(c);std::atomic<bool> cancel{false};DiscoveryTaskRequest q{1,w->clock->value,w->clock->value+100000,DiscoveryTaskKind::Discover,{}};CHECK(e->execute(q,cancel).snapshot.items.size()==2);CHECK(w->claims==2&&w->writes==0);});
  test("p10_engine_reprobe_same_handles_no_reopen_or_reclaim",[]{Rig r;r.first();const auto old=r.result.snapshot;const auto ports=r.handoff().ports;for(unsigned n=0;n<4;++n){auto next=r.again();CHECK(next.snapshot.items.size()==2);CHECK(next.snapshot.items[0].handle_nonce==old.items[0].handle_nonce&&next.snapshot.items[1].handle_nonce==old.items[1].handle_nonce);CHECK(next.snapshot.observed_us>r.result.snapshot.observed_us);auto *payload=dynamic_cast<NativeDiscoveryHandoff *>(next.payload.get());CHECK(payload&&payload->ports==ports);r.result=std::move(next);}CHECK(r.wire->opens==2&&r.wire->claims==2&&r.wire->enumerations==5&&r.wire->closes==0&&r.wire->writes==0);});
  test("p10_engine_unknown_alt0_never_claims",[]{auto w=std::make_shared<Wire>();auto c=config(w);c.usb_session_alt0=[](const auto&){return false;};auto e=make_native_discovery_engine(c);std::atomic<bool> cancelled{false};DiscoveryTaskRequest q{1,w->clock->value,w->clock->value+100000,DiscoveryTaskKind::Discover,{}};CHECK(e->execute(q,cancelled).snapshot.items.empty());CHECK(w->claims==0&&w->closes==2);});
  test("p10_engine_extra_usb_detected_without_open",[]{Rig r;r.first();r.wire->devices.push_back(descriptor(3));auto next=r.again();CHECK(next.snapshot.items.empty());CHECK(r.wire->opens==2&&r.wire->claims==2);CHECK(!r.handoff().ports[0]->active()&&!r.handoff().ports[1]->active());CHECK(r.wire->closes==0);r.engine->retire();CHECK(r.wire->closes==2);});
  test("p10_engine_missing_usb_entire_group_disabled",[]{Rig r;r.first();r.wire->devices.pop_back();CHECK(r.again().snapshot.items.empty());CHECK(!r.handoff().ports[0]->active()&&!r.handoff().ports[1]->active());CHECK(r.wire->closes==0);r.engine->retire();CHECK(r.wire->closes==2);});
  test("p10_engine_scan_error_no_active_group",[]{for(bool init:{false,true}){Rig r;r.first();if(init)r.wire->init_error=kUsbIo;else r.wire->enumerate_error=kUsbIo;CHECK(r.again().snapshot.items.empty());CHECK(!r.handoff().ports[0]->active());CHECK(r.wire->opens==2&&r.wire->closes==0);r.engine->retire();CHECK(r.wire->closes==2);}});
  test("p10_engine_scan_duplicate_or_bad_descriptor",[]{for(unsigned n=0;n<5;++n){Rig r;r.first();if(n==0)r.wire->devices[1].id=r.wire->devices[0].id;if(n==1)r.wire->devices[1].bcd_device=0x700;if(n==2)r.wire->devices[1].active_configuration_known=false;if(n==3)r.wire->devices[1].alternates[0].endpoints.push_back(r.wire->devices[1].alternates[0].endpoints[0]);if(n==4)r.wire->devices[1].bcd_device=0x601;CHECK(r.again().snapshot.items.empty());CHECK(r.wire->opens==2&&r.wire->closes==0);}});
  test("p10_engine_identity_replacement_retires_both",[]{for(unsigned n=0;n<5;++n){Rig r;r.first();auto &b=r.wire->boards[2];if(n==0)b.uid[0]++;if(n==1)b.board_type=7;if(n==2)b.protocol[0]++;if(n==3)b.application_signature[90]++;if(n==4)b.f4_capabilities[4]++;CHECK(r.again().snapshot.items.empty());CHECK(!r.handoff().ports[0]->active()&&!r.handoff().ports[1]->active());CHECK(r.wire->closes==0);r.engine->retire();CHECK(r.wire->closes==2);}});
  test("p10_engine_short_read_and_disconnect_fail_group",[]{for(bool short_read:{false,true}){Rig r;r.first();if(short_read)r.wire->short_request=0xc3;else r.wire->fail_request=0xc3;CHECK(r.again().snapshot.items.empty());CHECK(!r.handoff().ports[0]->active());CHECK(r.wire->closes==0);}});
  test("p10_engine_deadline_never_publishes_late_time",[]{Rig r;r.first();r.wire->call_cost=100000;CHECK(r.again().snapshot.items.empty());CHECK(!r.handoff().ports[0]->active());CHECK(r.wire->closes==0);});
  test("p10_engine_cancellation_disables_without_close_before_revoke",[]{Rig r;r.first();r.cancelled=true;CHECK(r.again().snapshot.items.empty());CHECK(!r.handoff().ports[0]->active());CHECK(r.wire->closes==0);r.engine->retire();CHECK(r.wire->closes==2);});
  test("p10_engine_readonly_port_blocks_all_writes",[]{Rig r;r.first();auto p=r.handoff().ports[0];const auto before=r.wire->requests.size();Request q;q.kind=Kind::ControlWrite;q.request=0xdc;CHECK(p->transact(q,1).status==Status::Invalid);q.kind=Kind::BulkWrite;q.endpoint=3;q.payload={1};CHECK(p->transact(q,1).status==Status::Invalid);q={};q.request=0xd8;q.read_length=1;CHECK(p->transact(q,1).status==Status::Invalid);CHECK(r.wire->requests.size()==before&&r.wire->writes==0);q.request=0xc3;q.read_length=12;CHECK(p->transact(q,1).status==Status::Ok);});
  test("p10_engine_port_outlives_real_lease_without_io_owner",[]{Rig r;r.first();auto p=r.handoff().ports[0];r.engine->retire();CHECK(r.wire->closes==2);Request q;q.request=0xc3;q.read_length=12;CHECK(p->transact(q,1).status==Status::Cancelled);CHECK(!p->live_spi_proof());p.reset();CHECK(r.wire->closes==2);});
  test("p10_engine_reconnect_both_new_nonces_after_retirement",[]{Rig r;r.first();const auto old=r.result.snapshot;r.engine->retire();CHECK(r.wire->closes==2);r.first();CHECK(r.result.snapshot.items[0].handle_nonce!=old.items[0].handle_nonce&&r.result.snapshot.items[1].handle_nonce!=old.items[1].handle_nonce);CHECK(r.wire->opens==4&&r.wire->claims==4);});
  test("p10_engine_discover_cannot_replace_unretired_group",[]{Rig r;r.first();const auto now=r.wire->clock->value;DiscoveryTaskRequest q{2,now,now+100000,DiscoveryTaskKind::Discover,{}};CHECK(r.engine->execute(q,r.cancelled).snapshot.items.empty());CHECK(!r.handoff().ports[0]->active());CHECK(r.wire->closes==0&&r.wire->opens==2);});
  test("p10_engine_unknown_spi_ownership_denied_before_usb_io",[]{auto w=std::make_shared<Wire>();auto c=config(w);auto &b=c.profile.slots[1];b.expected=raw(2);b.expected.serial.clear();b.expected.board_type=7;b.expected.f4_capabilities.clear();b.transport="SPI";b.exact_mcu="STM32H725IGK6";b.spi_selector={"/dev/spidev0.0",b.physical_evidence_sha256,true,153,0,0,8,50000000};auto e=make_native_discovery_engine(c);std::atomic<bool> cancel{false};DiscoveryTaskRequest q{1,w->clock->value,w->clock->value+100000,DiscoveryTaskKind::Discover,{}};CHECK(e->execute(q,cancel).snapshot.items.empty());CHECK(w->factories==0&&w->opens==0);});
  test("p10_engine_mixed_usb_spi_real_protocol_probe",[]{MixedRig r;r.first();CHECK(r.wire->opens==1&&r.wire->claims==1&&r.spi->opens==1);CHECK(r.handoff().profile.slots[1].expected.transport=="SPI");const auto proof=r.handoff().ports[1]->live_spi_proof();CHECK(proof&&proof->synchronized&&proof->same_fd_lifetime_lock&&!proof->kernel_deadline_proven);CHECK(proof->expected_uid==p10_spi_fixture::observation().uid);CHECK(r.result.snapshot.items[1].handle_nonce=="SPI:"+std::to_string(proof->lease_nonce));CHECK(r.result.slot_observed_us[0]!=r.result.slot_observed_us[1]);CHECK(r.result.snapshot.observed_us==std::min(r.result.slot_observed_us[0],r.result.slot_observed_us[1]));CHECK(p10_oracle_writes()==0);});
  test("p10_engine_mixed_reprobe_retains_spi_fd_fence_and_nonce",[]{MixedRig r;r.first();auto old=r.result.snapshot;const auto before=r.handoff().ports[1]->live_spi_proof();CHECK(before);auto next=r.again();CHECK(next.snapshot.items.size()==2);auto *h=dynamic_cast<NativeDiscoveryHandoff *>(next.payload.get());CHECK(h);const auto after=h->ports[1]->live_spi_proof();CHECK(after&&after->lease_nonce==before->lease_nonce&&after->first_fence_id==before->first_fence_id);CHECK(next.snapshot.items[0].handle_nonce==old.items[0].handle_nonce&&next.snapshot.items[1].handle_nonce==old.items[1].handle_nonce);CHECK(next.snapshot.observed_us>old.observed_us);CHECK(r.spi->opens==1&&r.spi->locks==2&&r.spi->unlocks==1&&r.spi->closes==0&&r.wire->opens==1&&r.wire->claims==1);CHECK(p10_oracle_writes()==0);});
  test("p10_engine_mixed_spi_fault_disables_before_owner_cleanup",[]{MixedRig r;r.first();r.spi->fail_next_message=true;CHECK(r.again().snapshot.items.empty());CHECK(!r.handoff().ports[0]->active()&&!r.handoff().ports[1]->active());CHECK(!r.handoff().ports[1]->live_spi_proof());CHECK(r.wire->closes==0&&r.spi->closes==0&&r.spi->unlocks==1);r.engine->retire();CHECK(r.wire->closes==1&&r.spi->closes==1&&r.spi->unlocks==2);});
  test("p10_engine_mixed_replacement_uid_not_rebound",[]{MixedRig r;r.first();auto uid=p10_spi_fixture::observation().uid;uid[0]^=1;r.spi->set_uid(uid);CHECK(r.again().snapshot.items.empty());CHECK(!r.handoff().ports[0]->active()&&!r.handoff().ports[1]->active());CHECK(r.spi->closes==0);r.engine->retire();CHECK(r.spi->closes==1);});
  test("p10_engine_mixed_same_seed_reconnect_fresh_pair",[]{MixedRig r;r.first();auto old=r.result.snapshot;const auto old_proof=r.handoff().ports[1]->live_spi_proof();CHECK(old_proof);r.engine->retire();r.first();const auto proof=r.handoff().ports[1]->live_spi_proof();CHECK(proof&&proof->first_fence_id==old_proof->first_fence_id&&proof->lease_nonce!=old_proof->lease_nonce);CHECK(r.result.snapshot.items[0].handle_nonce!=old.items[0].handle_nonce&&r.result.snapshot.items[1].handle_nonce!=old.items[1].handle_nonce);CHECK(r.spi->opens==2&&r.spi->closes==1&&r.spi->locks==4&&r.spi->unlocks==3);CHECK(p10_oracle_writes()==0);});
  std::cout<<"{\"suite\":\"p10_native_discovery_engine\",\"passed\":"<<passed<<",\"failed\":"<<failed<<",\"skipped\":0,\"assertions\":"<<assertions<<",\"real_device_io\":false}\n";return failed?1:0;
}
