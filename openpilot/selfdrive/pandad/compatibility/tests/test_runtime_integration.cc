#include "../runtime_integration.h"
#include <algorithm>
#include <cstring>
#include <cstdlib>
#include <deque>
#include <iostream>
#include <stdexcept>
#include <capnp/message.h>
#include <capnp/serialize.h>
#include "openpilot/cereal/gen/cpp/log.capnp.h"
#ifdef TEST_MSGQ_ADAPTER
#include "../msgq_adapter.h"
#include <filesystem>
#include <unistd.h>
#endif
using namespace openpilot::pandad::compatibility;
namespace {
using Bytes=MessageBytes;
std::size_t assertions=0;
void check(bool ok,int line) { ++assertions;if(!ok)throw std::runtime_error("integration line "+std::to_string(line)); }
#define CHECK(x) check(static_cast<bool>(x),__LINE__)
struct TestClock final:Clock { std::uint64_t now=1000000;std::uint64_t now_us()override{return now;}void wait_us(std::uint32_t n)override{now+=n;} };
Bytes pack(capnp::MallocMessageBuilder &builder){auto words=capnp::messageToFlatArray(builder);auto b=words.asBytes();return{b.begin(),b.end()};}
Bytes event(unsigned kind,std::uint64_t time,bool enabled=false){capnp::MallocMessageBuilder builder;auto e=builder.initRoot<cereal::Event>();e.setValid(true);e.setLogMonoTime(time*1000);
 if(kind==0){auto d=e.initDeviceState();d.setStarted(false);d.setFanSpeedPercentDesired(20);}
 if(kind==1)e.initSelfdriveState().setEnabled(enabled);
 if(kind==2)e.initCabinCameraState();
 if(kind==3)e.initSendcan(0);
 return pack(builder);}
Bytes params(unsigned model=19){capnp::MallocMessageBuilder builder;auto c=builder.initRoot<cereal::CarParams>().initSafetyConfigs(2);for(unsigned s=0;s<2;++s)c[s].setSafetyModel(static_cast<cereal::CarParams::SafetyModel>(model));return pack(builder);}
SessionProfile profile(bool spi=false){SessionProfile p;p.binding_status="SYNTHETIC_BOUND";
 for(unsigned s=0;s<2;++s){p.slots[s].expected={s?"fixture:red":"fixture:dos",s?"RED_PANDA":"DOS",s?"H7":"F4",s?"fixture:H7":"fixture:F4",spi&&s?"SPI":"USB",kSessionProtocol,{{0,1,2},s==1,s==1,s==1,true,s==0,s==0,false},"fixture:app"};p.slots[s].safety_config_semantics="noOutput";}
 p.roles={1,1,1,{0,1},0,0,kRoleUnsupported};return p;}
struct BoardState { unsigned opens=0,closes=0,transfers=0,tx=0,heartbeat_on=0;bool fail_open=false;std::vector<unsigned> controls;std::deque<Bytes> rx; };
struct FakeUsb final:LibusbApi {
 std::shared_ptr<BoardState> state;SessionIdentity identity;
 FakeUsb(std::shared_ptr<BoardState> s,SessionIdentity i):state(std::move(s)),identity(std::move(i)){}
 int initialize()override{return 0;}
 int enumerate(std::vector<UsbDeviceInfo>&out)override{out={{1,0xbbaa,0xddcc,static_cast<std::uint16_t>((identity.board_type=="DOS"?6:7)<<8),1,true,{{0,0,{{0x81,2,64},{2,2,64},{3,2,64}}}}}};return 0;}
 int open(UsbDeviceId,UsbHandle &h)override{++state->opens;h=1;return state->fail_open?kUsbIo:0;}
 int serial(UsbHandle,std::uint8_t,std::string &out,std::uint32_t)override{out=identity.identity;return 0;}
 int claim(UsbHandle,int)override{return 0;}
 int control(UsbHandle,std::uint8_t type,std::uint8_t request,std::uint16_t value,std::uint16_t,std::uint8_t *data,std::uint16_t length,std::uint32_t ms)override{
  CHECK(ms<=5);++state->transfers;state->controls.push_back(request);
  if(!(type&0x80)){CHECK(request==0xdc||request==0xdf||request==0xf3||request==0xb1);if(request==0xdc)CHECK(value==19);if(request==0xf3)state->heartbeat_on+=value!=0;return 0;}
  std::memset(data,0,length);
  if(request==0xd5){CHECK(length==16);const Bytes cap{'F','4','C','1',1,0,16,0,31,0,0,0,7,0,0,0};std::copy(cap.begin(),cap.end(),data);}
  else if(request==0xd2){CHECK(length==48);data[28]=1;data[30]=2;data[31]=19;data[47]=75;}
  else if(request==0xc2){CHECK(length==64);}
  else if(request==0xb2){CHECK(length==2);}
  else CHECK(false);
  return length;
 }
 int bulk(UsbHandle,std::uint8_t endpoint,std::uint8_t *data,int size,int &actual,std::uint32_t ms)override{CHECK(ms<=5);++state->transfers;actual=0;if(endpoint==3)++state->tx;if(endpoint==0x81&&!state->rx.empty()){const auto bytes=state->rx.front();state->rx.pop_front();CHECK(bytes.size()<=static_cast<unsigned>(size));std::copy(bytes.begin(),bytes.end(),data);actual=bytes.size();}return 0;}
 void release(UsbHandle,int)override{}void close(UsbHandle)override{++state->closes;}void shutdown()override{}
};
struct FakeSpi final:SpiIo {unsigned calls=0;Result exchange(const Bytes&,std::uint32_t)override{++calls;return{Status::Disconnected,0,{}};}};
struct Provider final:OfflineIoProvider {
 std::array<std::shared_ptr<BoardState>,2> state{{std::make_shared<BoardState>(),std::make_shared<BoardState>()}};unsigned calls=0,spi_calls=0;bool throws=false;
 std::unique_ptr<LibusbApi> usb(const DiscoveryItem &item)override{++calls;if(throws)throw std::runtime_error("provider failure");return std::make_unique<FakeUsb>(state[item.identity.board_type=="DOS"?0:1],item.identity);}
 std::unique_ptr<SpiIo> spi(const DiscoveryItem&)override{++spi_calls;return std::make_unique<FakeSpi>();}
};
struct Discovery final:DiscoveryProvider{DiscoverySnapshot value;unsigned calls=0;bool throws=false;DiscoverySnapshot snapshot()override{++calls;if(throws)throw std::runtime_error("discovery failure");return value;}};
struct Rig {
 TestClock clock;SessionProfile p;Discovery discovery;Provider io;InjectedTransportFactory factory{io,clock};std::array<std::deque<Bytes>,4> queues;
 RealtimeSource source;std::vector<std::pair<OutputTopic,Bytes>> publications;bool fail_sink=false;
 RealtimeSink sink;RuntimeIntegration owner;
 explicit Rig(bool spi=false):p(profile(spi)),source({[this]{return receive(0);},[this]{return receive(1);},[this]{return receive(2);},[this]{return receive(3);}}),
 sink([this](OutputTopic topic,const Bytes &bytes){publications.emplace_back(topic,bytes);return !fail_sink;}),owner(p,clock,discovery,factory,source,sink){fresh();}
 std::optional<Bytes> receive(unsigned channel){if(queues[channel].empty())return std::nullopt;auto out=std::move(queues[channel].front());queues[channel].pop_front();return out;}
 void fresh(){++discovery.value.revision;discovery.value.observed_us=clock.now;discovery.value.items.clear();for(unsigned s=0;s<2;++s)discovery.value.items.push_back({p.slots[s].expected,"nonce-"+std::to_string(discovery.value.revision)+"-"+std::to_string(s),true,static_cast<std::uint32_t>(1+discovery.value.revision)});}
 void cycle(){++clock.now;discovery.value.observed_us=clock.now;owner.cycle();}
 void ready(){CHECK(owner.refresh());cycle();++clock.now;queues[0].push_back(event(0,clock.now));cycle();CHECK(source.submit_params(params(),owner.capture()));cycle();for(unsigned i=0;i<20;++i)cycle();CHECK(owner.snapshot().panda_states_valid);}
};
}
int main(){std::vector<std::string> cases;const auto test=[&](const char *name,const std::function<void()> &fn){fn();cases.push_back(name);std::cout<<"PASS "<<name<<'\n';};try{
 test("p8_owner_exact_binding_real_usb_backend_health_cereal",[]{Rig r;r.ready();CHECK(r.io.calls==2);CHECK(!r.owner.snapshot().real_tx_authorized);CHECK(!r.owner.snapshot().simulated_tx_allowed);CHECK(r.publications.size()>20);CHECK(r.io.state[0]->tx+r.io.state[1]->tx==0);});
 test("p8_discovery_order_never_assigns_roles",[]{Rig r;std::reverse(r.discovery.value.items.begin(),r.discovery.value.items.end());CHECK(r.owner.refresh());CHECK(r.owner.sessions().slots[0].binding.expected.board_type=="DOS");CHECK(r.owner.sessions().slots[1].binding.expected.board_type=="RED_PANDA");});
 test("p8_identity_all_fields_rejected_before_factory",[]{for(unsigned n=0;n<8;++n){Rig r;auto &id=r.discovery.value.items[0].identity;switch(n){case 0:id.identity="fixture:replacement";break;case 1:id.board_type="RED_PANDA";break;case 2:id.mcu_family="H7";break;case 3:id.exact_mcu="fixture:other";break;case 4:id.transport="SPI";break;case 5:id.protocol_profile="old";break;case 6:id.application_signature="fixture:other";break;default:id.capabilities.fan=false;}CHECK(!r.owner.refresh());CHECK(r.io.calls==0);}});
 test("p8_duplicate_extra_and_missing_inventory_fail_closed",[]{for(unsigned n=0;n<3;++n){Rig r;if(n==0)r.discovery.value.items[1]=r.discovery.value.items[0];if(n==1)r.discovery.value.items.push_back(r.discovery.value.items[0]);if(n==2)r.discovery.value.items.pop_back();CHECK(!r.owner.refresh());CHECK(r.io.calls==0);}});
 test("p8_discovery_stale_future_revision_rollback",[]{for(unsigned n=0;n<4;++n){Rig r;CHECK(r.owner.refresh());if(n==0)r.discovery.value.observed_us=r.clock.now+1;if(n==1)r.clock.now+=kLoopMaxGapUs;if(n==2)r.discovery.value.revision=0;if(n==3)r.discovery.value.observed_us=0;CHECK(!r.owner.refresh());CHECK(!r.owner.sessions().group_available);CHECK(r.io.state[0]->closes==1);}});
 test("p8_partial_open_cancels_owner_and_consumes_both_nonces",[]{Rig r;r.io.state[1]->fail_open=true;CHECK(!r.owner.refresh());CHECK(r.io.state[0]->closes==1);r.io.state[1]->fail_open=false;CHECK(!r.owner.refresh());CHECK(r.io.calls==2);r.fresh();CHECK(r.owner.refresh());});
 test("p8_reconnect_requires_fresh_pair_and_new_epochs",[]{Rig r;CHECK(r.owner.refresh());const auto old=r.owner.capture();const auto original=r.discovery.value.items;r.discovery.value.items.pop_back();CHECK(!r.owner.refresh());r.discovery.value.items=original;++r.discovery.value.revision;CHECK(!r.owner.refresh());r.fresh();CHECK(r.owner.refresh());CHECK(r.owner.capture().generation>old.generation);CHECK(r.io.calls==4);});
 test("p8_revision_reuse_cannot_swap_handle",[]{Rig r;CHECK(r.owner.refresh());r.discovery.value.items[0].handle_nonce="swapped";CHECK(!r.owner.refresh());CHECK(r.io.calls==2);});
 test("p8_unbound_profile_never_calls_discovery",[]{TestClock c;Discovery d;Provider p;InjectedTransportFactory f(p,c);RealtimeSource source({[]{return std::optional<Bytes>{};},[]{return std::optional<Bytes>{};},[]{return std::optional<Bytes>{};},[]{return std::optional<Bytes>{};}});RealtimeSink sink([](auto,const auto&){return true;});RuntimeIntegration owner(SessionProfile{},c,d,f,source,sink);CHECK(!owner.refresh());owner.cycle();CHECK(d.calls==0&&p.calls==0);});
 test("p8_nooutput_rejects_sendcan_and_enabled_before_transfer",[]{for(unsigned kind:{1U,3U}){Rig r;r.ready();++r.clock.now;r.queues[kind].push_back(event(kind,r.clock.now,true));const auto cycle=r.owner.cycle();CHECK(cycle.callback_failed);CHECK(!r.owner.snapshot().simulated_tx_allowed);CHECK(r.io.state[0]->tx+r.io.state[1]->tx==0);CHECK(r.io.state[0]->heartbeat_on+r.io.state[1]->heartbeat_on==0);}});
 test("p8_wrong_topic_and_malformed_cereal_close_gate",[]{for(bool malformed:{false,true}){Rig r;r.ready();r.queues[0].push_back(malformed?Bytes{1,2,3}:event(2,r.clock.now));CHECK(r.owner.cycle().callback_failed);CHECK(!r.owner.snapshot().simulated_tx_allowed);}});
 test("p8_epoch_fence_drops_old_queue_and_carparams",[]{Rig r;CHECK(r.owner.refresh());r.cycle();const auto old=r.owner.capture();CHECK(r.source.submit_params(params(),old));r.queues[0].push_back(event(0,r.clock.now));r.fresh();CHECK(r.owner.refresh());r.cycle();CHECK(r.source.discarded()==2);CHECK(!r.owner.snapshot().panda_states_valid);});
 test("p8_fence_backlog_bounded_and_fail_closed",[]{Rig r;for(unsigned n=0;n<25;++n)r.queues[0].push_back(event(0,r.clock.now));const auto cycle=r.owner.cycle();CHECK(cycle.callback_failed);CHECK(r.queues[0].size()==8);CHECK(!r.owner.snapshot().simulated_tx_allowed);});
 test("p8_params_slot_nooutput_safety_not_relaxed",[]{Rig r;CHECK(r.owner.refresh());r.cycle();++r.clock.now;r.queues[0].push_back(event(0,r.clock.now));r.cycle();CHECK(r.source.submit_params(params(35),r.owner.capture()));const auto cycle=r.owner.cycle();CHECK(cycle.rejected==1);CHECK(!r.owner.snapshot().simulated_tx_allowed);});
 test("p8_shutdown_is_owner_serialized_cancels_no_rediscovery",[]{Rig r;r.ready();r.owner.request_shutdown();const auto calls=r.discovery.calls;CHECK(r.owner.cycle().shutdown);CHECK(r.discovery.calls==calls);CHECK(!r.owner.sessions().group_available);CHECK(r.io.state[0]->closes==1&&r.io.state[1]->closes==1);CHECK(!r.owner.refresh());});
 test("p8_provider_and_publication_failure_close_group",[]{Rig a;a.io.throws=true;CHECK(!a.owner.refresh());CHECK(!a.owner.sessions().group_available);Rig b;b.ready();b.fail_sink=true;CHECK(b.owner.cycle().callback_failed);CHECK(!b.owner.snapshot().simulated_tx_allowed);});
 test("p8_mixed_spi_usb_factory_inert_until_cycle",[]{Rig r(true);CHECK(r.owner.refresh());CHECK(r.io.calls==1&&r.io.spi_calls==1);CHECK(r.io.state[0]->transfers==0);r.cycle();CHECK(!r.owner.snapshot().panda_states_valid);CHECK(!r.owner.snapshot().simulated_tx_allowed);});
 test("p8_factory_denies_alt_and_missing_spi_nonce",[]{Rig r;r.discovery.value.items[0].usb_alt0_verified=false;CHECK(!r.owner.refresh());CHECK(r.io.calls==0);Rig s(true);s.discovery.value.items[1].spi_first_request_id=0;CHECK(!s.owner.refresh());CHECK(s.io.spi_calls==0);});
 test("p8_activation_evidence_revocation_retires_same_nonce",[]{Rig r;CHECK(r.owner.refresh());r.discovery.value.items[0].usb_alt0_verified=false;CHECK(!r.owner.refresh());CHECK(r.io.calls==2);CHECK(r.io.state[0]->closes==1);Rig s(true);CHECK(s.owner.refresh());++s.discovery.value.items[1].spi_first_request_id;CHECK(!s.owner.refresh());CHECK(s.io.spi_calls==1);});
 test("p8_late_camera_from_previous_epoch_rejected",[]{Rig r;r.ready();const auto old_time=r.clock.now;++r.clock.now;r.fresh();CHECK(r.owner.refresh());r.cycle();r.queues[2].push_back(event(2,old_time));const auto result=r.owner.cycle();CHECK(result.rejected==1);CHECK(!r.owner.snapshot().simulated_tx_allowed);});
 test("p8_ingress_bound_params_and_sink_type",[]{Rig r;CHECK(!r.source.submit_params(Bytes(65544),{}));CHECK(r.source.submit_params(params(),{}));CHECK(!r.source.submit_params(params(),{}));bool rejected=false;try{r.sink.publish(OutputTopic::Can,event(0,r.clock.now));}catch(...){rejected=true;}CHECK(rejected);});
 test("p8_dual_usb_wire_to_actual_cereal_preserves_global_bus",[]{Rig r;r.ready();for(unsigned slot=0;slot<2;++slot){const CanFrame frame{0x11f+slot,slot,false,false,false,false,Bytes{1,2,3,4,5}};auto wire=encode_can_frame(frame,CanCapability::ClassicOnly,FrameDirection::Rx);CHECK(wire);r.io.state[slot]->rx.push_back(wire.value);}r.cycle();const auto &bytes=r.publications.back().second;auto words=kj::heapArray<capnp::word>(bytes.size()/8);std::memcpy(words.begin(),bytes.data(),bytes.size());capnp::FlatArrayMessageReader reader(words);auto e=reader.getRoot<cereal::Event>();CHECK(e.isCan()&&e.getValid());auto frames=e.getCan();CHECK(frames.size()==2);CHECK(frames[0].getSrc()==0&&frames[1].getSrc()==5);CHECK(frames[0].getAddress()==0x11f&&frames[1].getAddress()==0x120);CHECK(frames[0].getDat().size()==5&&frames[1].getDat()[4]==5);});
 test("p8_nooutput_preserves_rx_fragment_across_cycles",[]{Rig r;r.ready();const CanFrame frame{0x11f,0,false,false,false,false,Bytes{1,2,3,4,5}};const auto wire=encode_can_frame(frame,CanCapability::ClassicOnly,FrameDirection::Rx);CHECK(wire);r.io.state[0]->rx.push_back(Bytes(wire.value.begin(),wire.value.begin()+3));r.cycle();r.io.state[0]->rx.push_back(Bytes(wire.value.begin()+3,wire.value.end()));r.cycle();const auto &bytes=r.publications.back().second;auto words=kj::heapArray<capnp::word>(bytes.size()/8);std::memcpy(words.begin(),bytes.data(),bytes.size());capnp::FlatArrayMessageReader reader(words);auto e=reader.getRoot<cereal::Event>();CHECK(e.isCan()&&e.getValid()&&e.getCan().size()==1);CHECK(e.getCan()[0].getAddress()==0x11f);CHECK(r.io.state[0]->tx+r.io.state[1]->tx==0);});
 test("p8_reconnect_discards_old_rx_fragment",[]{Rig r;r.ready();const CanFrame frame{0x11f,0,false,false,false,false,Bytes{1,2,3,4,5}};const auto wire=encode_can_frame(frame,CanCapability::ClassicOnly,FrameDirection::Rx);CHECK(wire);r.io.state[0]->rx.push_back(Bytes(wire.value.begin(),wire.value.begin()+3));r.cycle();++r.clock.now;r.fresh();r.ready();r.io.state[0]->rx.push_back(Bytes(wire.value.begin()+3,wire.value.end()));r.cycle();const auto &bytes=r.publications.back().second;auto words=kj::heapArray<capnp::word>(bytes.size()/8);std::memcpy(words.begin(),bytes.data(),bytes.size());capnp::FlatArrayMessageReader reader(words);auto e=reader.getRoot<cereal::Event>();CHECK(e.isCan()&&e.getCan().size()==0);CHECK(r.io.state[0]->tx+r.io.state[1]->tx==0);});
 test("p8_owner_consumed_handle_history_is_bounded",[]{Rig r;for(unsigned n=0;n<64;++n){if(n)r.fresh();CHECK(r.owner.refresh());}CHECK(r.io.calls==128);r.fresh();CHECK(!r.owner.refresh());CHECK(r.io.calls==128);CHECK(!r.owner.sessions().group_available);});
 test("p8_owner_destruction_cancels_before_backend_release",[]{std::array<std::shared_ptr<BoardState>,2> state;unsigned before=0;{Rig r;r.ready();state=r.io.state;before=state[0]->transfers+state[1]->transfers;}CHECK(state[0]->closes==1&&state[1]->closes==1);CHECK(state[0]->transfers+state[1]->transfers==before);});
 test("p8_discovery_exception_retires_live_handles",[]{Rig r;r.ready();r.discovery.throws=true;CHECK(!r.owner.refresh());CHECK(!r.owner.sessions().group_available);CHECK(r.io.state[0]->closes==1&&r.io.state[1]->closes==1);});
 test("p8_actual_cereal_stale_future_inputs_rejected",[]{for(bool future:{false,true}){Rig r;r.ready();r.queues[0].push_back(event(0,future?r.clock.now+1:r.clock.now-kDeviceInputMaxAgeUs));auto cycle=r.owner.cycle();CHECK(cycle.rejected==1);CHECK(!r.owner.snapshot().simulated_tx_allowed);}});
#ifdef TEST_MSGQ_ADAPTER
 test("p8_actual_msgq_interface_nonblocking_owned_copy",[]{struct Socket:SubSocket{std::deque<Bytes> q;int connect(Context*,std::string,std::string,bool,bool,std::size_t)override{return 0;}void setTimeout(int)override{}Message*receive(bool nonblocking)override{CHECK(nonblocking);if(q.empty())return nullptr;auto m=new Message();m->init(reinterpret_cast<char*>(q.front().data()),q.front().size());q.pop_front();return m;}} socket;auto receive=bind_msgq_receiver(socket);CHECK(!receive());socket.q.push_back(event(0,1000000));auto bytes=receive();CHECK(bytes&&*bytes==event(0,1000000));bool rejected=false;try{bind_msgq_publishers({{nullptr,nullptr,nullptr}});}catch(...){rejected=true;}CHECK(rejected);});
 test("p8_actual_msgq_private_shm_bidirectional",[]{
  CHECK(std::getenv("P8_ISOLATED_MSGQ") && std::string(std::getenv("P8_ISOLATED_MSGQ"))=="1");
  const std::string prefix="p8-"+std::to_string(getpid());const std::filesystem::path dir="/dev/shm/msgq_"+prefix;
  CHECK(std::filesystem::create_directory(dir));CHECK(setenv("OPENPILOT_PREFIX",prefix.c_str(),1)==0);CHECK(unsetenv("CEREAL_FAKE")==0);
  {
   Context context;std::array<std::unique_ptr<PubSocket>,3> pubs;std::array<std::unique_ptr<SubSocket>,3> subs;
   std::array<PubSocket*,3> raw{};
   for(unsigned i=0;i<3;++i){const auto name="out-"+std::to_string(i);pubs[i].reset(PubSocket::create(&context,name,false,65536));subs[i].reset(SubSocket::create(&context,name,"127.0.0.1",false,false,65536));CHECK(pubs[i]&&subs[i]);raw[i]=pubs[i].get();}
   RealtimeSink sink(bind_msgq_publishers(raw));
   for(unsigned i=0;i<3;++i){capnp::MallocMessageBuilder builder;auto e=builder.initRoot<cereal::Event>();e.setLogMonoTime(1000000000);e.setValid(false);if(i==0)e.initCan(0);if(i==1)e.initPandaStates(2);if(i==2)e.initPeripheralState();const auto expected=pack(builder);CHECK(sink.publish(static_cast<OutputTopic>(i),expected));auto receive=bind_msgq_receiver(*subs[i]);auto actual=receive();CHECK(actual&&*actual==expected);CHECK(!receive());}
   std::array<std::unique_ptr<PubSocket>,4> inpubs;std::array<std::unique_ptr<SubSocket>,4> insubs;std::array<ReceiveEvent,4> channels;
   for(unsigned i=0;i<4;++i){const auto name="in-"+std::to_string(i);inpubs[i].reset(PubSocket::create(&context,name,false,65536));insubs[i].reset(SubSocket::create(&context,name,"127.0.0.1",false,false,65536));CHECK(inpubs[i]&&insubs[i]);channels[i]=bind_msgq_receiver(*insubs[i]);}
   RealtimeSource source(channels);CHECK(!source.next({1,1}));auto bytes=event(0,1000000);CHECK(inpubs[0]->send(reinterpret_cast<char*>(bytes.data()),bytes.size())==static_cast<int>(bytes.size()));auto received=source.next({1,1});CHECK(received&&received->bytes==bytes&&received->epoch.generation==1);
   CHECK(inpubs[0]->send(reinterpret_cast<char*>(bytes.data()),bytes.size())==static_cast<int>(bytes.size()));CHECK(!source.next({2,2}));CHECK(source.discarded()==1);CHECK(!source.next({2,2}));
  }
  CHECK(unsetenv("OPENPILOT_PREFIX")==0);CHECK(std::filesystem::remove_all(dir)==8);
 });
#endif
 std::cout<<"{\"suite\":\"p8_runtime_integration\",\"passed\":"<<cases.size()<<",\"failed\":0,\"skipped\":0,\"assertions\":"<<assertions<<",\"real_device_io\":false,\"real_can_tx\":false,\"schema_changed\":false}\n";return 0;
}catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}}
