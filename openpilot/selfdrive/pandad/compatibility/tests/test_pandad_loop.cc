#include "../pandad_loop.h"

#include <algorithm>
#include <cstring>
#include <deque>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <capnp/message.h>
#include <capnp/serialize.h>
#include "openpilot/cereal/gen/cpp/log.capnp.h"

using namespace openpilot::pandad::compatibility;
namespace {
std::size_t assertions = 0;
void check(bool yes, int line) { ++assertions; if (!yes) throw std::runtime_error("loop assertion line " + std::to_string(line)); }
#define CHECK(x) check(static_cast<bool>(x), __LINE__)
using Bytes = MessageBytes;
void put16(Bytes &b, unsigned n, unsigned value) { b[n] = value; b[n+1] = value >> 8; }
SessionProfile profile() {
  SessionProfile p; p.binding_status = "SYNTHETIC_BOUND";
  for (unsigned s=0;s<2;++s) {
    p.slots[s].expected = {s ? "fixture:red":"fixture:dos", s ? "RED_PANDA":"DOS",s ? "H7":"F4",
      s ? "fixture:H7":"fixture:F4","USB",kSessionProtocol,{{0,1,2},s==1,s==1,s==1,true,s==0,s==0,false},"fixture:app"};
    p.slots[s].safety_config_semantics = "noOutput";
  }
  p.roles={1,1,1,{0,1},0,0,kRoleUnsupported}; return p;
}
struct FakeClock final : Clock {
  std::uint64_t now = 1000000;
  std::uint64_t now_us() override { return now; }
  void wait_us(std::uint32_t amount) override { now += amount; }
};
struct Board final : Backend {
  FakeClock &clock; unsigned param=0,alternative=0,delay=0;
  std::vector<Request> calls;
  explicit Board(FakeClock &c) : clock(c) {}
  Result transact(const Request &r,std::uint32_t timeout) override {
    CHECK(timeout<=5);calls.push_back(r);clock.now+=delay;
    if(r.kind==Kind::ControlWrite) {
      CHECK(r.request==0xdc||r.request==0xdf||r.request==0xf3||r.request==0xb1);
      if(r.request==0xdc){CHECK(r.value==19);param=r.index;}
      if(r.request==0xdf){alternative=r.value;}
      return {Status::Ok,0,{}};
    }
    if(r.kind==Kind::BulkWrite){return {Status::Ok,r.payload.size(),{}};}
    if(r.kind==Kind::BulkRead){return {Status::Short,0,{}};}
    Bytes b(r.read_length);
    if(r.request==0xd2){b[28]=1;b[30]=2;b[31]=19;put16(b,32,param);put16(b,35,alternative);b[47]=75;}
    else if(r.request==0xc2){put16(b,41,500);put16(b,43,2000);}
    else if(r.request==0xb2){put16(b,0,1200);}
    else throw std::runtime_error("unexpected control request");
    return {Status::Ok,b.size(),b};
  }
};
Bytes pack(capnp::MallocMessageBuilder &b) {auto a=capnp::messageToFlatArray(b);auto bytes=a.asBytes();return{bytes.begin(),bytes.end()};}
Bytes event(const char *name,std::uint64_t time,bool enabled=false) {
  capnp::MallocMessageBuilder b;auto e=b.initRoot<cereal::Event>();e.setValid(true);e.setLogMonoTime(time*1000);
  if(std::string(name)=="device"){auto d=e.initDeviceState();d.setStarted(enabled);d.setFanSpeedPercentDesired(25);}
  else if(std::string(name)=="selfdrive"){e.initSelfdriveState().setEnabled(enabled);}
  else {auto f=e.initSendcan(1)[0];f.setAddress(0x123);f.setSrc(4);const std::uint8_t data[]{1};f.setDat(kj::arrayPtr(data,1));}
  return pack(b);
}
Bytes parameters() {
  capnp::MallocMessageBuilder b;auto c=b.initRoot<cereal::CarParams>().initSafetyConfigs(2);
  for(unsigned i=0;i<2;++i){c[i].setSafetyModel(cereal::CarParams::SafetyModel::NO_OUTPUT);c[i].setSafetyParam(i+1);}
  return pack(b);
}
void inspect(const Bytes &b,const std::function<void(cereal::Event::Reader)> &fn) {
  auto a=kj::heapArray<capnp::word>(b.size()/8);std::memcpy(a.begin(),b.data(),b.size());
  capnp::FlatArrayMessageReader r(a);fn(r.getRoot<cereal::Event>());
}
struct Source final : EventSource {
  std::deque<InputEnvelope> queue;std::vector<InputEpoch> captures;
  std::function<void()> before;bool throws=false;
  std::optional<InputEnvelope> next(InputEpoch captured) override {
    captures.push_back(captured);
    if(before){before();}
    if(throws){throw std::runtime_error("source failure");}
    if(queue.empty()){return std::nullopt;}
    auto e=std::move(queue.front());queue.pop_front();return e;
  }
};
struct Sink final : EventSink {
  std::vector<std::pair<OutputTopic,Bytes>> records;bool fail=false,throws=false;
  std::function<void()> before;
  bool publish(OutputTopic t,const Bytes &b) override {
    if(before){before();}
    if(throws){throw std::runtime_error("sink failure");}
    records.emplace_back(t,b);return !fail;
  }
  unsigned count(OutputTopic topic)const{return std::count_if(records.begin(),records.end(),[&](const auto &r){return r.first==topic;});}
};
struct Rig {
  FakeClock clock;SessionProfile p=profile();Board b0{clock},b1{clock};Transport t0{b0,clock},t1{b1,clock};
  PandadRuntime runtime{p,clock};Source source;Sink sink;PandadLoop loop{runtime,clock,source,sink};
  Rig(){CHECK(runtime.reconcile({{p.slots[0].expected,1},{p.slots[1].expected,1}},{{&t0,&t1}}));}
  void push(Bytes bytes,InputKind kind=InputKind::Event){source.queue.push_back({kind,std::move(bytes),runtime.capture()});}
  void ready(){
    ++clock.now;CHECK(ingest_cereal_event(runtime,event("device",clock.now),runtime.capture()));
    CHECK(configure_cereal_params(runtime,parameters(),runtime.capture()));
    for(unsigned i=0;i<16;++i){runtime.tick();clock.now+=1000;}CHECK(runtime.snapshot().panda_states_valid);
    for(unsigned i=0;i<3;++i){runtime.peripheral_step();}
  }
  void engage(){
    ++clock.now;CHECK(ingest_cereal_event(runtime,event("selfdrive",clock.now,false),runtime.capture()));
    ++clock.now;CHECK(ingest_cereal_event(runtime,event("device",clock.now,true),runtime.capture()));
    ++clock.now;CHECK(ingest_cereal_event(runtime,event("selfdrive",clock.now,true),runtime.capture()));
    CHECK(runtime.snapshot().simulated_tx_allowed);
  }
};
}
int main(){
  std::vector<std::string> passed;
  const auto test=[&](const char *name,const std::function<void()> &fn){fn();passed.push_back(name);std::cout<<"PASS "<<name<<'\n';};
  try{
    test("loop_fixed_publication_cadence_and_serialized_outputs",[]{
      Rig r;
      for(unsigned i=0;i<=50;++i){const auto c=r.loop.cycle();CHECK(c.inputs==0);CHECK(!c.budget_exceeded);r.clock.now+=10000;}
      CHECK(r.sink.count(OutputTopic::Can)==51);CHECK(r.sink.count(OutputTopic::PandaStates)==6);CHECK(r.sink.count(OutputTopic::PeripheralState)==2);
      for(const auto &record:r.sink.records){const auto topic=record.first;inspect(record.second,[&](auto e){CHECK(!e.getValid());
        if(topic==OutputTopic::PandaStates){CHECK(e.isPandaStates());CHECK(e.getPandaStates().size()==2);}
        if(topic==OutputTopic::Can){CHECK(e.isCan());}
        if(topic==OutputTopic::PeripheralState){CHECK(e.isPeripheralState());}});}
    });
    test("loop_max16_inputs_and_control_before_ingress",[]{
      Rig r;r.ready();r.clock.now+=100000;
      const auto before=r.b0.calls.size()+r.b1.calls.size();
      r.source.before=[&]{CHECK(r.b0.calls.size()+r.b1.calls.size()>before);};
      for(unsigned i=0;i<20;++i){r.push(event("selfdrive",r.clock.now+i,false));}
      const auto c=r.loop.cycle();CHECK(c.inputs==16);CHECK(c.accepted+c.rejected==16);CHECK(r.source.queue.size()==4);
      CHECK(c.receive_attempts<=2&&c.transmit_attempts<=2);CHECK(r.source.captures.size()==16);
    });
    test("loop_buffered_epoch_not_retagged_after_fault",[]{
      Rig r;r.ready();r.engage();r.push(event("sendcan",r.clock.now));const auto old=r.source.queue.front().epoch;
      r.runtime.invalid_input("TEST_FAULT");const auto changed=r.runtime.capture();CHECK(old.control_epoch!=changed.control_epoch);
      r.source.queue.front().bytes={1,2,3};const auto c=r.loop.cycle();CHECK(c.inputs==1&&c.rejected==1);
      CHECK(r.source.captures.front().control_epoch==changed.control_epoch);CHECK(r.runtime.capture().control_epoch==changed.control_epoch);
      CHECK(r.runtime.counters().simulated_tx==0);
    });
    test("loop_carparams_barrier_rejects_preconfiguration_enable",[]{
      Rig r;++r.clock.now;CHECK(ingest_cereal_event(r.runtime,event("device",r.clock.now),r.runtime.capture()));
      r.push(parameters(),InputKind::CarParams);r.push(event("selfdrive",r.clock.now,true));
      const auto c=r.loop.cycle();CHECK(c.inputs==2&&c.accepted==1&&c.rejected==1);CHECK(!r.runtime.snapshot().simulated_tx_allowed);
    });
    test("loop_dual_receive_tx_attempts_and_routed_sendcan",[]{
      Rig r;r.ready();r.engage();++r.clock.now;r.push(event("sendcan",r.clock.now));
      const auto c=r.loop.cycle();CHECK(c.accepted==1);CHECK(c.receive_attempts==2&&c.transmit_attempts==2);
      CHECK(r.runtime.counters().simulated_tx==1);CHECK(!r.runtime.snapshot().real_tx_authorized);
      unsigned tx0=0,tx1=0;for(const auto &q:r.b0.calls){tx0+=q.kind==Kind::BulkWrite;}
      for(const auto &q:r.b1.calls){tx1+=q.kind==Kind::BulkWrite;}
      CHECK(tx0==0&&tx1==1);
    });
    test("loop_gap_exact_threshold_invalidates_before_old_enable",[]{
      for(auto gap:{kLoopMaxGapUs-1,kLoopMaxGapUs,kLoopMaxGapUs+1}){Rig r;r.loop.cycle();r.clock.now+=gap;
        r.push(event("selfdrive",r.clock.now,true));const auto c=r.loop.cycle();CHECK(c.overdue==(gap>=kLoopMaxGapUs));
        CHECK(!r.runtime.snapshot().simulated_tx_allowed);if(c.overdue){CHECK(c.rejected==1);}
      }
    });
    test("loop_observed_budget_before_input_and_exact_expiry",[]{
      for(auto delay:{kLoopBudgetUs-1,kLoopBudgetUs,kLoopBudgetUs+1}){Rig r;
        bool delayed=false;r.source.before=[&]{if(!delayed){r.clock.now+=delay;delayed=true;}};
        r.push(event("selfdrive",r.clock.now,true));const auto c=r.loop.cycle();
        CHECK(c.budget_exceeded==(delay>=kLoopBudgetUs));CHECK(!r.runtime.snapshot().simulated_tx_allowed);
        if(c.budget_exceeded){CHECK(c.inputs==1&&c.rejected==1);CHECK(c.publications==3);CHECK(c.receive_attempts==0);}
        // A callback that completes just before the bound may be consumed.
        else {CHECK(c.inputs==1);}
      }
    });
    test("loop_peripheral_phase_only_every50ms",[]{
      Rig r;unsigned attempts=0;for(unsigned i=0;i<=10;++i){attempts+=r.loop.cycle().peripheral_attempted;r.clock.now+=10000;}
      CHECK(attempts==3);
      for(auto *board:{&r.b0,&r.b1}){for(const auto &call:board->calls){CHECK(call.request!=0xb1&&call.request!=0xb2&&call.request!=0xb0);}}
    });
    test("loop_sink_failure_and_source_exception_close_group",[]{
      for(unsigned mode=0;mode<3;++mode){Rig r;r.ready();r.engage();
        r.source.throws=mode==0;r.sink.fail=mode==1;r.sink.throws=mode==2;const auto c=r.loop.cycle();
        CHECK(c.callback_failed);CHECK(!r.runtime.snapshot().simulated_tx_allowed);CHECK(c.inputs==0);
      }
    });
    test("loop_backward_clock_latches_closed_and_publication_overflow_bounded",[]{
      Rig r;r.loop.cycle();--r.clock.now;CHECK(r.loop.cycle().clock_failed);r.clock.now+=10000;CHECK(r.loop.cycle().clock_failed);
      CHECK(r.source.captures.size()==1);CHECK(!r.runtime.snapshot().simulated_tx_allowed);
      Rig huge;huge.clock.now=UINT64_MAX;const auto c=huge.loop.cycle();CHECK(c.callback_failed);CHECK(c.publications==0);
    });
    std::cout<<"{\"suite\":\"pandad_loop\",\"passed\":"<<passed.size()<<",\"failed\":0,\"skipped\":0,\"assertions\":"<<assertions
      <<",\"device_access\":false,\"real_can_tx\":false,\"schema_changed\":false}\n";
    return 0;
  }catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}
}
