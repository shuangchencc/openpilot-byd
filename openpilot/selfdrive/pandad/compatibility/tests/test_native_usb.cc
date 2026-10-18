#include "../native_usb.h"

#include <algorithm>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <stdexcept>

using namespace openpilot::pandad::compatibility;
namespace {
unsigned assertions = 0;
#define CHECK(x) do { ++assertions; if (!(x)) throw std::runtime_error(std::string(__func__) + ": " + #x); } while (false)
struct FakeClock : Clock {
  std::uint64_t value = 1000000;
  std::uint64_t now_us() override { return value; }
  void wait_us(std::uint32_t n) override { value += n; }
};
UsbDeviceInfo descriptor(unsigned id) {
  return {id, 0xbbaa, 0xddcc, 0x600, 3, true,
    {{0, 0, {{0x81, 2, 64}, {0x02, 2, 64}, {0x03, 2, 64}}},
     {0, 1, {{0x81, 3, 64}, {0x02, 2, 64}, {0x03, 2, 64}}}}};
}
NativeUsbObservation observation(unsigned id) {
  NativeUsbObservation result;
  result.serial = "fixture-usb-" + std::to_string(id);
  result.uid.fill(static_cast<std::uint8_t>(id)); result.board_type = 6;
  result.protocol = {0xf5, 0x12, 0xbc, 0xff, 0x76, 0xf2, 0xab, 0x75};
  result.application_signature.fill(static_cast<std::uint8_t>(id + 10));
  result.f4_capabilities = {'F','4','C','1',1,0,16,0,31,0,0,0,7,0,0,0};
  return result;
}
struct State {
  std::vector<UsbDeviceInfo> descriptors{descriptor(1)};
  std::map<UsbHandle, NativeUsbObservation> wire{{1, observation(1)}};
  std::map<unsigned, int> override_count;
  std::vector<unsigned> requests, types, transfer_handles;
  std::vector<UsbHandle> closed;
  unsigned initialized = 0, enumerated = 0, opened = 0, serials = 0, claimed = 0, released = 0, shutdown = 0, bulk_calls = 0;
  int init_error = 0, enumerate_error = 0, open_error = 0, serial_error = 0, claim_error = 0;
  UsbDeviceId fail_open_id = 0;
  int bulk_error = 0, bulk_actual = 4;
  bool zero_handle = false;
  std::function<void(const char *)> callback;
  void call(const char *s) { if (callback) callback(s); }
};
class FakeApi : public LibusbApi {
public:
  explicit FakeApi(std::shared_ptr<State> s) : state(std::move(s)) {}
  int initialize() override { ++state->initialized; state->call("init"); return state->init_error; }
  int enumerate(std::vector<UsbDeviceInfo> &out) override { ++state->enumerated; out = state->descriptors; state->call("enumerate"); return state->enumerate_error; }
  int open(UsbDeviceId id, UsbHandle &handle) override {
    ++state->opened; state->call("open");
    const int error = state->fail_open_id == id ? kUsbAccess : state->open_error;
    handle = error || state->zero_handle ? 0 : id; return error;
  }
  int serial(UsbHandle h, std::uint8_t index, std::string &out, std::uint32_t timeout) override {
    CHECK(index == 3 && timeout > 0 && timeout <= 5); ++state->serials;
    out = state->wire.at(h).serial; state->call("serial"); return state->serial_error;
  }
  int claim(UsbHandle, int interface) override { CHECK(interface == 0); ++state->claimed; state->call("claim"); return state->claim_error; }
  int control(UsbHandle h, std::uint8_t type, std::uint8_t req, std::uint16_t, std::uint16_t,
              std::uint8_t *data, std::uint16_t length, std::uint32_t timeout) override {
    CHECK(timeout > 0 && timeout <= 5); state->requests.push_back(req); state->types.push_back(type); state->transfer_handles.push_back(h);
    const auto &w = state->wire.at(h); std::vector<std::uint8_t> bytes;
    if (type == 0x40) { CHECK(length == 0); state->call("control"); return 0; }
    CHECK(type == 0xc0);
    switch (req) {
      case 0xc3: bytes.assign(w.uid.begin(), w.uid.end()); break;
      case 0xc1: bytes = {w.board_type}; break;
      case 0xdd: bytes.assign(w.protocol.begin(), w.protocol.end()); break;
      case 0xd3: bytes.assign(w.application_signature.begin(), w.application_signature.begin()+64); break;
      case 0xd4: bytes.assign(w.application_signature.begin()+64, w.application_signature.end()); break;
      case 0xd5: bytes = w.f4_capabilities; break;
      default: bytes.resize(length, 7); break;
    }
    std::copy_n(bytes.begin(), std::min<std::size_t>(length, bytes.size()), data);
    state->call("control");
    return state->override_count.count(req) ? state->override_count.at(req) : static_cast<int>(bytes.size());
  }
  int bulk(UsbHandle h, std::uint8_t endpoint, std::uint8_t *data, int length, int &actual, std::uint32_t timeout) override {
    CHECK(timeout > 0 && timeout <= 5 && (endpoint == 0x81 || endpoint == 0x03));
    ++state->bulk_calls; state->transfer_handles.push_back(h); actual = state->bulk_actual;
    if (endpoint == 0x81) std::fill_n(data, std::min(length, std::max(0, actual)), static_cast<std::uint8_t>(h));
    state->call("bulk"); return state->bulk_error;
  }
  void release(UsbHandle, int n) override { CHECK(n == 0); ++state->released; }
  void close(UsbHandle h) override { state->closed.push_back(h); }
  void shutdown() override { ++state->shutdown; }
private:
  std::shared_ptr<State> state;
};
NativeUsbProbePolicy policy(const std::shared_ptr<State> &s, bool f4 = true) {
  NativeUsbProbePolicy p; p.expected_count = s->descriptors.size();
  if (f4) for (const auto &[id,w] : s->wire) p.f4_application_signatures.push_back(w.application_signature);
  return p;
}
NativeUsbInventory discover(const std::shared_ptr<State> &s, FakeClock &c, bool f4 = true) {
  return discover_native_usb(std::make_unique<FakeApi>(s), c, policy(s, f4), true);
}
NativeUsbActivation grant(const NativeUsbObservation &o, bool writes = false, bool tx = false) {
  NativeUsbActivation g; g.lease_nonce = o.lease_nonce; g.allow_device_io = true; g.profile_validated = true;
  g.bulk_alt0_verified = true; g.allow_control_writes = writes; g.allow_can_tx = tx; return g;
}
std::unique_ptr<Backend> active(const std::shared_ptr<State> &s, FakeClock &c, bool writes = false, bool tx = false) {
  auto found = discover(s,c); CHECK(found.complete()); const auto o = found.observations()[0];
  auto result = found.activate(0,o,grant(o,writes,tx),c); CHECK(result); return result;
}
Request bulk_read() { Request r; r.kind = Kind::BulkRead; r.endpoint = 0x81; r.read_length = 8; return r; }
Request bulk_write() { Request r; r.kind = Kind::BulkWrite; r.endpoint = 0x03; r.payload = {1,2,3,4}; return r; }
Request control_write(unsigned req, unsigned value = 0) { Request r; r.kind = Kind::ControlWrite; r.request = req; r.value = value; return r; }
Request control_read(unsigned req, unsigned size) { Request r; r.kind = Kind::ControlRead; r.request = req; r.read_length = size; return r; }

void inert_and_default_denied() {
  FakeClock c; auto s=std::make_shared<State>();
  { NativeUsbInventory empty; CHECK(!empty.complete()); }
  { auto found=discover_native_usb(std::make_unique<FakeApi>(s),c,policy(s)); CHECK(!found.complete()); }
  { auto found=discover_native_usb(c,{}); CHECK(!found.complete()); } // Links native implementation; zero device calls.
  CHECK(s->initialized==0&&s->opened==0&&s->shutdown==0);
}
void invalid_probe_policy() {
  for(unsigned n=0;n<6;++n){FakeClock c;auto s=std::make_shared<State>();auto p=policy(s);
    if(n==0)p.expected_count=0;else if(n==1)p.expected_count=3;else if(n==2)p.budget_ms=0;else if(n==3)p.budget_ms=101;
    else if(n==4)p.f4_application_signatures.push_back(p.f4_application_signatures[0]);else p.f4_application_signatures[0].fill(0);
    auto found=discover_native_usb(std::make_unique<FakeApi>(s),c,p,true);CHECK(!found.complete());CHECK(s->initialized==0);}
}
void full_probe_readonly_no_claim() {
  FakeClock c;auto s=std::make_shared<State>();
  {auto found=discover(s,c);CHECK(found.complete());CHECK(found.reason()=="OBSERVED_NOT_TRUSTED");const auto o=found.observations()[0];
   CHECK(o.lease_nonce!=0&&o.uid==s->wire[1].uid&&o.application_signature==s->wire[1].application_signature);
   CHECK(s->requests==std::vector<unsigned>({0xc3,0xc1,0xdd,0xd3,0xd4,0xd5}));
   CHECK(std::all_of(s->types.begin(),s->types.end(),[](auto t){return t==0xc0;}));
   CHECK(s->claimed==0&&s->bulk_calls==0&&s->closed.empty());}
  CHECK(s->closed==std::vector<UsbHandle>{1}&&s->released==0&&s->shutdown==1);
}
void unknown_signature_does_not_infer_f4() {
  FakeClock c;auto s=std::make_shared<State>();auto found=discover(s,c,false);CHECK(found.complete());
  CHECK(found.observations()[0].f4_capabilities.empty());CHECK(std::count(s->requests.begin(),s->requests.end(),0xd5)==0);
}
void exact_reply_lengths() {
  for(auto req:{0xc3,0xc1,0xdd,0xd3,0xd4,0xd5})for(auto count:{0,127,-7}){
    FakeClock c;auto s=std::make_shared<State>();s->override_count[req]=count;auto found=discover(s,c);
    CHECK(!found.complete());CHECK(s->claimed==0&&s->closed.size()==1&&s->shutdown==1);}
}
void malformed_identity_and_protocol() {
  for(unsigned n=0;n<9;++n){FakeClock c;auto s=std::make_shared<State>();auto &w=s->wire[1];
    switch(n){case 0:w.serial="";break;case 1:w.serial="bad serial";break;case 2:w.serial=std::string(129,'a');break;
    case 3:w.uid.fill(0);break;case 4:w.uid.fill(255);break;case 5:w.board_type=7;break;case 6:w.protocol[0]^=1;break;
    case 7:w.application_signature.fill(0);break;default:w.f4_capabilities[4]=2;}
    auto p=policy(s,false);if(n!=7)p.f4_application_signatures={w.application_signature};
    auto found=discover_native_usb(std::make_unique<FakeApi>(s),c,p,true);CHECK(!found.complete());CHECK(s->claimed==0);}
}
void malformed_descriptors() {
  for(unsigned n=0;n<11;++n){FakeClock c;auto s=std::make_shared<State>();auto &d=s->descriptors[0];
    switch(n){case 0:d.id=0;break;case 1:d.active_configuration_known=false;break;case 2:d.serial_index=0;break;
    case 3:d.bcd_device=0x601;break;case 4:d.alternates.clear();break;case 5:d.alternates.push_back(d.alternates[0]);break;
    case 6:d.alternates[0].endpoints[0].attributes=3;break;case 7:d.alternates[0].endpoints[0].max_packet_size=512;break;
    case 8:d.alternates[0].endpoints.pop_back();break;case 9:d.alternates[0].endpoints.push_back(d.alternates[0].endpoints[0]);break;
    default:d.alternates.resize(kUsbMaxAlternates+1);}
    auto found=discover(s,c);CHECK(!found.complete());CHECK(s->opened==0);}
}
void inaccessible_incomplete_inventory() {
  for(unsigned n=0;n<6;++n){FakeClock c;auto s=std::make_shared<State>();
    if(n==0)s->init_error=kUsbAccess;else if(n==1)s->enumerate_error=kUsbIo;else if(n==2)s->open_error=kUsbAccess;
    else if(n==3)s->serial_error=kUsbNoDevice;else if(n==4)s->zero_handle=true;else s->fail_open_id=1;
    auto found=discover(s,c);CHECK(!found.complete());CHECK(s->claimed==0);}
}
void unexpected_extra_candidate_denied() {
  FakeClock c;auto s=std::make_shared<State>();auto p=policy(s);s->descriptors.push_back(descriptor(2));s->wire[2]=observation(2);
  auto found=discover_native_usb(std::make_unique<FakeApi>(s),c,p,true);CHECK(!found.complete());CHECK(s->opened==0);
}
void bounded_inventory_and_descriptor_storage() {
  FakeClock c;auto s=std::make_shared<State>();auto p=policy(s);s->descriptors.resize(kUsbMaxInventoryDevices+1,descriptor(1));
  auto found=discover_native_usb(std::make_unique<FakeApi>(s),c,p,true);CHECK(!found.complete());CHECK(s->opened==0);
}
void bootstub_dfu_and_unrelated_never_opened() {
  FakeClock c;auto s=std::make_shared<State>();auto p=policy(s);auto d=descriptor(2);d.product=0xddee;s->descriptors.push_back(d);
  d.id=3;d.vendor=0x0483;d.product=0xdf11;s->descriptors.push_back(d);d.id=4;d.vendor=0x1234;d.product=0x5678;s->descriptors.push_back(d);
  auto found=discover_native_usb(std::make_unique<FakeApi>(s),c,p,true);CHECK(found.complete());CHECK(s->opened==1);
}
void duplicate_serial_uid_device_denied() {
  for(unsigned n=0;n<3;++n){FakeClock c;auto s=std::make_shared<State>();s->descriptors.push_back(descriptor(2));s->wire[2]=observation(2);
    if(n==0)s->wire[2].serial=s->wire[1].serial;else if(n==1)s->wire[2].uid=s->wire[1].uid;else s->descriptors[1].id=1;
    auto found=discover(s,c);CHECK(!found.complete());CHECK(s->closed.size()==s->opened&&s->shutdown==1);}
}
void full_group_failure_releases_first_lease() {
  FakeClock c;auto s=std::make_shared<State>();s->descriptors.push_back(descriptor(2));s->wire[2]=observation(2);s->fail_open_id=2;
  auto found=discover(s,c);CHECK(!found.complete());CHECK(s->closed==std::vector<UsbHandle>{1});CHECK(s->shutdown==1);
}
void discovery_deadline_and_clock_rollback() {
  for(auto stage:{"init","enumerate","open","serial","control"})for(bool rollback:{false,true}){
    FakeClock c;auto s=std::make_shared<State>();s->callback=[&](const char *name){if(std::string(name)==stage)c.value=rollback?0:2000000;};
    auto found=discover(s,c);CHECK(!found.complete());CHECK(found.status()==Status::Timeout);CHECK(s->claimed==0);}
}
void activation_missing_evidence_does_not_claim() {
  for(unsigned n=0;n<5;++n){FakeClock c;auto s=std::make_shared<State>();auto found=discover(s,c);auto o=found.observations()[0];auto g=grant(o);
    if(n==0)g.allow_device_io=false;else if(n==1)g.profile_validated=false;else if(n==2)g.bulk_alt0_verified=false;
    else if(n==3)g.lease_nonce=0;else ++g.lease_nonce;
    CHECK(!found.activate(0,o,g,c));CHECK(s->claimed==0);CHECK(!found.complete());CHECK(s->closed.size()==1);}
}
void expected_identity_is_not_serial_only() {
  for(unsigned n=0;n<7;++n){FakeClock c;auto s=std::make_shared<State>();auto found=discover(s,c);auto o=found.observations()[0];auto g=grant(o);
    switch(n){case 0:o.serial+="x";break;case 1:o.uid[0]^=1;break;case 2:o.board_type=7;break;case 3:o.protocol[0]^=1;break;
    case 4:o.application_signature[100]^=1;break;case 5:o.f4_capabilities[0]^=1;break;default:++o.lease_nonce;}
    CHECK(!found.activate(0,o,g,c));CHECK(s->claimed==0);}
}
void activation_reprobes_same_retained_handle() {
  FakeClock c;auto s=std::make_shared<State>();auto found=discover(s,c);auto o=found.observations()[0];
  auto backend=found.activate(0,o,grant(o),c);CHECK(backend);CHECK(s->enumerated==1&&s->opened==1&&s->serials==2&&s->claimed==1);
  CHECK(s->requests.size()==12);CHECK(!found.activate(0,o,grant(o),c));
  CHECK(std::all_of(s->transfer_handles.begin(),s->transfer_handles.end(),[](auto h){return h==1;}));
  backend.reset();CHECK(s->released==1&&s->closed.size()==1&&s->shutdown==1);
}
void activation_replacement_identity_rejected() {
  for(unsigned n=0;n<6;++n){FakeClock c;auto s=std::make_shared<State>();auto found=discover(s,c);auto o=found.observations()[0];
    auto &w=s->wire[1];switch(n){case 0:w.serial+="x";break;case 1:w.uid[0]^=1;break;case 2:w.board_type=7;break;
    case 3:w.protocol[0]^=1;break;case 4:w.application_signature[100]^=1;break;default:w.f4_capabilities[4]=2;}
    CHECK(!found.activate(0,o,grant(o),c));CHECK(s->enumerated==1&&s->opened==1&&s->claimed==1&&s->released==1&&s->closed.size()==1);}
}
void activation_claim_failure_and_deadline() {
  for(unsigned n=0;n<3;++n){FakeClock c;auto s=std::make_shared<State>();auto found=discover(s,c);auto o=found.observations()[0];
    if(n==0)s->claim_error=kUsbBusy;else s->callback=[&](const char *stage){if(std::string(stage)==(n==1?"claim":"control"))c.value+=200000;};
    CHECK(!found.activate(0,o,grant(o),c));CHECK(s->opened==1&&s->closed.size()==1&&s->shutdown==1);}
}
void fresh_nonce_never_reuses_usb_address() {
  FakeClock c;auto s=std::make_shared<State>();std::uint64_t old=0;
  {auto found=discover(s,c);old=found.observations()[0].lease_nonce;}
  auto found=discover(s,c);auto o=found.observations()[0];CHECK(o.lease_nonce>old);auto g=grant(o);g.lease_nonce=old;
  CHECK(!found.activate(0,o,g,c));CHECK(s->claimed==0);
}
void readonly_backend_denies_mutating_control_in_and_out() {
  FakeClock c;auto s=std::make_shared<State>();auto b=active(s,c);auto before=s->requests.size();
  for(auto req:{0xc0,0xc5,0xd8,0xdb,0xdc,0xdf,0xf3})CHECK(b->transact(control_read(req,8),5).status==Status::Invalid);
  CHECK(b->transact(control_write(0xdc,19),5).status==Status::Invalid);CHECK(b->transact(bulk_write(),5).status==Status::Invalid);
  CHECK(s->requests.size()==before&&s->bulk_calls==0);
}
void runtime_write_allowlist_and_nooutput_limits() {
  FakeClock c;auto s=std::make_shared<State>();auto b=active(s,c,true);auto before=s->requests.size();
  for(auto r:{control_write(0xdc,19),control_write(0xdf),control_write(0xf3),control_write(0xb1,100),control_write(0xb0,50)})
    CHECK(b->transact(r,5).status==Status::Ok);
  CHECK(s->requests.size()==before+5);
  for(auto r:{control_write(0xdc,0),control_write(0xdc,17),control_write(0xdf,1),control_write(0xf3,1),
      control_write(0xb1,101),control_write(0xb0,51),control_write(0xd8),control_write(0xc0)})CHECK(b->transact(r,5).status==Status::Invalid);
  auto r=control_write(0xdc,19);r.index=1;CHECK(b->transact(r,5).status==Status::Invalid);r.index=0;r.payload={1};CHECK(b->transact(r,5).status==Status::Invalid);
  CHECK(s->requests.size()==before+5);CHECK(b->transact(bulk_write(),5).status==Status::Invalid);
}
void runtime_read_allowlist_and_bounds() {
  FakeClock c;auto s=std::make_shared<State>();auto b=active(s,c);auto before=s->requests.size();
  CHECK(b->transact(control_read(0xd2,48),5).status==Status::Ok);
  auto r=control_read(0xc2,64);r.value=2;CHECK(b->transact(r,5).status==Status::Ok);r.value=3;CHECK(b->transact(r,5).status==Status::Invalid);
  CHECK(b->transact(control_read(0xc3,13),5).status==Status::Invalid);r=control_read(0xdd,8);r.index=1;CHECK(b->transact(r,5).status==Status::Invalid);
  CHECK(b->transact(bulk_read(),0).status==Status::Invalid);CHECK(b->transact(bulk_read(),6).status==Status::Invalid);
  CHECK(s->requests.size()==before+2&&s->bulk_calls==0);
}
void bulk_short_partial_timeout_and_no_replay() {
  FakeClock c;auto s=std::make_shared<State>();auto b=active(s,c,false,true);
  auto r=b->transact(bulk_read(),5);CHECK(r.status==Status::Short&&r.actual==4&&r.data==std::vector<std::uint8_t>(4,1));
  s->bulk_error=kUsbTimeout;s->bulk_actual=2;r=b->transact(bulk_read(),5);CHECK(r.status==Status::Timeout&&r.actual==2&&r.actual_known);
  r=b->transact(bulk_write(),5);CHECK(r.status==Status::Timeout&&r.actual==2&&r.delivery_uncertain);CHECK(s->bulk_calls==3);
}
void unknown_control_count_poisoned() {
  FakeClock c;auto s=std::make_shared<State>();auto b=active(s,c);s->override_count[0xd2]=kUsbTimeout;
  const auto r=b->transact(control_read(0xd2,48),5);CHECK(r.status==Status::Timeout&&!r.actual_known);auto count=s->requests.size();
  CHECK(b->transact(bulk_read(),5).status==Status::Poisoned);CHECK(s->requests.size()==count&&s->bulk_calls==0);
}
void overflow_and_disconnect_permanent_no_reopen() {
  for(unsigned n=0;n<3;++n){FakeClock c;auto s=std::make_shared<State>();auto b=active(s,c);
    if(n==0)s->bulk_error=kUsbOverflow;else if(n==1)s->bulk_actual=9;else s->bulk_error=kUsbNoDevice;
    auto r=b->transact(bulk_read(),5);CHECK(r.status==(n==2?Status::Disconnected:Status::Poisoned));
    CHECK(b->transact(bulk_read(),5).status==Status::Poisoned);CHECK(s->bulk_calls==1&&s->opened==1&&s->enumerated==1);}
}
void two_board_lease_isolation_and_cancellation() {
  FakeClock c;auto s=std::make_shared<State>();s->descriptors.push_back(descriptor(2));s->wire[2]=observation(2);auto found=discover(s,c);
  CHECK(found.complete());auto o=found.observations();auto a=found.activate(0,o[0],grant(o[0]),c);auto b=found.activate(1,o[1],grant(o[1]),c);
  CHECK(a&&b);a->cancel();CHECK(a->transact(bulk_read(),5).status==Status::Cancelled);auto r=b->transact(bulk_read(),5);
  CHECK(r.data==std::vector<std::uint8_t>(4,2));CHECK(s->bulk_calls==1);a.reset();CHECK(s->shutdown==0&&s->closed==std::vector<UsbHandle>{1});
  b.reset();CHECK(s->shutdown==1&&s->closed==std::vector<UsbHandle>({1,2}));
}
void readonly_complete_inventory_does_not_bind_or_activate() {
  FakeClock c;auto s=std::make_shared<State>();auto found=discover(s,c);CHECK(found.complete());
  CHECK(found.status()==Status::Ok&&found.reason()=="OBSERVED_NOT_TRUSTED");CHECK(s->claimed==0&&s->bulk_calls==0);
  // This type intentionally has no MCU, physical capability, authenticated
  // firmware, vehicle permission or profile-bound fields to infer from USB.
}
void actual_probe_and_activation_completion_times() {
  FakeClock c;auto s=std::make_shared<State>();s->callback=[&](const char *){c.value+=100;};
  auto found=discover(s,c);CHECK(found.complete());auto o=found.observations()[0];
  CHECK(o.observed_us==c.value&&o.observed_us>1000000);
  auto later=o;later.observed_us+=500000;CHECK(later==o); // Time cannot alter raw software identity.
  c.value+=10000;std::uint64_t activated=0;
  auto backend=found.activate(0,o,grant(o),c,&activated);CHECK(backend);
  CHECK(activated==c.value&&activated>o.observed_us);
  CHECK(found.observations()[0].observed_us==o.observed_us); // Discovery timestamp is never refreshed in place.
  c.value+=10000;CHECK(activated!=c.value);
}
void failed_activation_never_returns_fresh_time() {
  for(unsigned n=0;n<4;++n){FakeClock c;auto s=std::make_shared<State>();auto found=discover(s,c);auto o=found.observations()[0];auto g=grant(o);
    if(n==0)g.bulk_alt0_verified=false;else if(n==1)s->claim_error=kUsbBusy;else if(n==2)s->wire[1].uid[0]^=1;
    else s->callback=[&](const char *stage){if(std::string(stage)=="control")c.value+=200000;};
    std::uint64_t activated=12345;CHECK(!found.activate(0,o,g,c,&activated));CHECK(activated==0);}
  FakeClock c;NativeUsbInventory empty;std::uint64_t time=123;CHECK(!empty.activate(0,{}, {},c,&time));CHECK(time==0);
}
}  // namespace
int main() {
  const std::vector<std::pair<const char *,std::function<void()>>> cases{
    {"inert_and_default_denied",inert_and_default_denied},{"invalid_probe_policy",invalid_probe_policy},
    {"full_probe_readonly_no_claim",full_probe_readonly_no_claim},{"unknown_signature_does_not_infer_f4",unknown_signature_does_not_infer_f4},
    {"exact_reply_lengths",exact_reply_lengths},{"malformed_identity_and_protocol",malformed_identity_and_protocol},
    {"malformed_descriptors",malformed_descriptors},{"inaccessible_incomplete_inventory",inaccessible_incomplete_inventory},
    {"unexpected_extra_candidate_denied",unexpected_extra_candidate_denied},{"bounded_inventory_and_descriptor_storage",bounded_inventory_and_descriptor_storage},
    {"bootstub_dfu_and_unrelated_never_opened",bootstub_dfu_and_unrelated_never_opened},{"duplicate_serial_uid_device_denied",duplicate_serial_uid_device_denied},
    {"full_group_failure_releases_first_lease",full_group_failure_releases_first_lease},{"discovery_deadline_and_clock_rollback",discovery_deadline_and_clock_rollback},
    {"activation_missing_evidence_does_not_claim",activation_missing_evidence_does_not_claim},{"expected_identity_is_not_serial_only",expected_identity_is_not_serial_only},
    {"activation_reprobes_same_retained_handle",activation_reprobes_same_retained_handle},{"activation_replacement_identity_rejected",activation_replacement_identity_rejected},
    {"activation_claim_failure_and_deadline",activation_claim_failure_and_deadline},{"fresh_nonce_never_reuses_usb_address",fresh_nonce_never_reuses_usb_address},
    {"readonly_backend_denies_mutating_control_in_and_out",readonly_backend_denies_mutating_control_in_and_out},{"runtime_write_allowlist_and_nooutput_limits",runtime_write_allowlist_and_nooutput_limits},
    {"runtime_read_allowlist_and_bounds",runtime_read_allowlist_and_bounds},{"bulk_short_partial_timeout_and_no_replay",bulk_short_partial_timeout_and_no_replay},
    {"unknown_control_count_poisoned",unknown_control_count_poisoned},{"overflow_and_disconnect_permanent_no_reopen",overflow_and_disconnect_permanent_no_reopen},
    {"two_board_lease_isolation_and_cancellation",two_board_lease_isolation_and_cancellation},{"readonly_complete_inventory_does_not_bind_or_activate",readonly_complete_inventory_does_not_bind_or_activate},
    {"actual_probe_and_activation_completion_times",actual_probe_and_activation_completion_times},{"failed_activation_never_returns_fresh_time",failed_activation_never_returns_fresh_time},
  };
  unsigned passed=0,failed=0;
  for(const auto &[name,run]:cases){try{run();++passed;std::cout<<"PASS p9_usb_"<<name<<"\n";}catch(const std::exception&e){++failed;std::cerr<<"FAIL p9_usb_"<<name<<": "<<e.what()<<"\n";}}
  std::cout<<"{\"suite\":\"p9_native_usb\",\"passed\":"<<passed<<",\"failed\":"<<failed<<",\"skipped\":0,\"assertions\":"<<assertions<<",\"real_device_io\":false}\n";
  return failed?1:0;
}
