#include "offline_entry.h"
#include "cereal_bridge.h"
#include "pandad_loop.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>

#include <capnp/message.h>
#include <capnp/serialize.h>
#include "openpilot/cereal/gen/cpp/log.capnp.h"

namespace openpilot::pandad::compatibility {
namespace {
using Bytes = std::vector<std::uint8_t>;
std::size_t assertions = 0;
void check(bool yes, int line) { ++assertions; if (!yes) throw std::runtime_error("runtime fixture assertion at line " + std::to_string(line)); }
#define CHECK(x) check(static_cast<bool>(x), __LINE__)
void put16(Bytes &b, unsigned offset, unsigned n) { b[offset] = n; b[offset+1] = n >> 8; }
SessionProfile fixture(unsigned dos = 0) {
  SessionProfile p; p.binding_status = "SYNTHETIC_BOUND";
  for (unsigned slot = 0; slot < 2; ++slot) {
    const bool internal = slot == dos;
    auto &b = p.slots[slot];
    b.expected = {internal ? "fixture:dos" : "fixture:external", internal ? "DOS" : "RED_PANDA",
      internal ? "F4" : "H7", internal ? "fixture:unknown-F4" : "fixture:unknown-H7", "USB", kSessionProtocol,
      {{0,1,2}, !internal, !internal, !internal, true, internal, internal, false},
      internal ? "fixture:dos-app" : "fixture:red-app"}; b.safety_config_semantics = "noOutput";
  }
  p.roles = {int(1-dos), int(1-dos), int(1-dos), {0,1}, int(dos), int(dos), kRoleUnsupported};
  return p;
}
class FakeClock final : public Clock {
public:
  std::uint64_t time = 1000000;
  std::uint64_t now_us() override { return time; }
  void wait_us(std::uint32_t duration) override { time += duration; }
};
class FakeBoard final : public Backend {
public:
  FakeBoard(FakeClock &c, bool internal) : clock(c), dos(internal) {}
  FakeClock &clock; bool dos; unsigned param = 0, alternative = 0, model = 19;
  unsigned bad_request = 0, delay_us = 0; bool uncertain_write = false, mismatched = false;
  Bytes can_bytes;
  std::vector<Request> calls;
  Result transact(const Request &r, std::uint32_t timeout_ms) override {
    CHECK(timeout_ms <= 5); calls.push_back(r); clock.time += delay_us;
    if (uncertain_write && r.kind == Kind::BulkWrite) return {Status::Timeout, 0, {}, false, true};
    if (r.kind == Kind::ControlWrite) {
      CHECK(r.request == 0xdc || r.request == 0xdf || r.request == 0xf3 || r.request == 0xb1 || r.request == 0xb0);
      if (r.request == 0xdc) { CHECK(r.value == 19); model=r.value;param=r.index; }
      if (r.request == 0xdf) alternative=r.value;
      return {Status::Ok,0,{}};
    }
    if (r.kind == Kind::BulkWrite) return {Status::Ok,r.payload.size(),{}};
    if (r.kind == Kind::BulkRead) { auto data=can_bytes;can_bytes.clear(); return {Status::Short,data.size(),data}; }
    Bytes bytes(r.read_length);
    if (r.request == 0xd5) {
      CHECK(dos); bytes = {0x46,0x34,0x43,0x31,1,0,16,0,0x1f,0,0,0,7,0,0,0};
    } else if (r.request == 0xd2) {
      bytes[0]=42; put16(bytes,4,dos ? 12001:13002); put16(bytes,6,dos ? 321:654);
      bytes[28]=dos ? 0:1; bytes[30]=dos ? 0:2;bytes[31]=model;
      put16(bytes,32,mismatched ? param+1:param);put16(bytes,35,alternative);bytes[47]=75;
      put16(bytes,39,77);put16(bytes,45,1234);
    } else if (r.request == 0xc2) { put16(bytes,41,500); put16(bytes,43,2000); }
    else if (r.request == 0xb2) put16(bytes,0,2345);
    else throw std::runtime_error("unexpected request");
    if (r.request == bad_request && !bytes.empty()) bytes.pop_back();
    return {bytes.size() == r.read_length ? Status::Ok:Status::Short,bytes.size(),bytes};
  }
};
Bytes pack(capnp::MallocMessageBuilder &b) { auto words=capnp::messageToFlatArray(b);auto bytes=words.asBytes();return {bytes.begin(),bytes.end()}; }
void trace_message(const Bytes &bytes, const char *topic, unsigned dos, std::uint64_t now, bool valid) {
  static constexpr char hex[] = "0123456789abcdef";
  std::string encoded; encoded.reserve(bytes.size()*2);
  for (const auto byte : bytes) { encoded += hex[byte>>4]; encoded += hex[byte&15]; }
  std::cout << "TRACE CEREAL {\"topic\":\"" << topic << "\",\"dos_slot\":" << dos
            << ",\"now_ns\":" << now*1000 << ",\"expected_valid\":" << (valid ? "true" : "false")
            << ",\"hex\":\"" << encoded << "\"}\n";
}
void read_event(const Bytes &bytes, const std::function<void(cereal::Event::Reader)> &fn) {
  auto words=kj::heapArray<capnp::word>(bytes.size()/8);std::memcpy(words.begin(),bytes.data(),bytes.size());
  capnp::FlatArrayMessageReader reader(words);fn(reader.getRoot<cereal::Event>());
}
Bytes params(unsigned n=2) {
  capnp::MallocMessageBuilder b;auto cp=b.initRoot<cereal::CarParams>();cp.setAlternativeExperience(2);auto configs=cp.initSafetyConfigs(n);
  for(unsigned i=0;i<n;++i){configs[i].setSafetyModel(cereal::CarParams::SafetyModel::NO_OUTPUT);configs[i].setSafetyParam(100+i);}
  return pack(b);
}
Bytes state_event(const char *kind, std::uint64_t time, bool value, bool valid=true) {
  capnp::MallocMessageBuilder b;auto e=b.initRoot<cereal::Event>();e.setLogMonoTime(time*1000);e.setValid(valid);
  if(std::string(kind)=="device") {auto ds=e.initDeviceState();ds.setStarted(value);ds.setFanSpeedPercentDesired(37);}
  else e.initSelfdriveState().setEnabled(value);
  return pack(b);
}
Bytes send_event(std::uint64_t time, bool valid=true, unsigned source=4) {
  capnp::MallocMessageBuilder b;auto e=b.initRoot<cereal::Event>();e.setLogMonoTime(time*1000);e.setValid(valid);
  auto f=e.initSendcan(1)[0];f.setAddress(0x321);f.setSrc(source);const std::uint8_t data[]{1,2,3};f.setDat(kj::arrayPtr(data,3));return pack(b);
}
struct Rig {
  FakeClock clock;
  SessionProfile profile;
  FakeBoard b0,b1;
  Transport t0,t1;
  PandadRuntime runtime;
  explicit Rig(unsigned dos=0):Rig(fixture(dos)) {}
  explicit Rig(const SessionProfile &p):profile(p),b0(clock,p.slots[0].expected.board_type=="DOS"),
    b1(clock,p.slots[1].expected.board_type=="DOS"),t0(b0,clock),t1(b1,clock),runtime(profile,clock) {
    CHECK(runtime.reconcile({{profile.slots[1].expected,1},{profile.slots[0].expected,1}},{{&t0,&t1}}));
  }
  bool input(const Bytes &bytes) {return ingest_cereal_event(runtime,bytes,runtime.capture());}
  void device(bool started=false) {++clock.time;CHECK(input(state_event("device",clock.time,started)));}
  void ready() {
    device();CHECK(configure_cereal_params(runtime,params(),runtime.capture()));
    for(unsigned i=0;i<16;++i) {runtime.tick();clock.time+=1000;}
    CHECK(runtime.snapshot().configured);CHECK(runtime.snapshot().panda_states_valid);
    for (unsigned i=0;i<3;++i) runtime.peripheral_step();
  }
  void engage() {
    ++clock.time;CHECK(input(state_event("selfdrive",clock.time,false)));
    device(true);++clock.time;CHECK(input(state_event("selfdrive",clock.time,true)));
    CHECK(runtime.snapshot().simulated_tx_allowed);CHECK(!runtime.snapshot().real_tx_authorized);
  }
};
}
int pandad_compat_offline_main() {
  assertions=0;
  std::vector<std::string> passed;
  const auto test=[&](const char *name,const std::function<void()> &fn){fn();passed.push_back(name);std::cout<<"PASS "<<name<<'\n';};
  try {
    test("real_cereal_fixed_two_slots_and_both_role_orders",[]{
      for(unsigned dos=0;dos<2;++dos){Rig r(dos);r.ready();
        trace_message(serialize_panda_states(r.runtime,r.clock.time),"pandaStates",dos,r.clock.time,true);
        read_event(serialize_panda_states(r.runtime,r.clock.time),[&](auto e){CHECK(e.getValid());auto ps=e.getPandaStates();CHECK(ps.size()==2);
          CHECK(ps[dos].getPandaType()==cereal::PandaState::PandaType::DOS);CHECK(ps[dos].getVoltage()==12001);CHECK(ps[dos].getCurrent()==0);
          CHECK(ps[1-dos].getCurrent()==654);
          CHECK(ps[dos].getSpiErrorCount()==0);CHECK(ps[dos].getSoundOutputLevel()==0);
          CHECK(ps[1-dos].getSpiErrorCount()==77);CHECK(ps[1-dos].getSoundOutputLevel()==1234);
          CHECK(ps[1-dos].getHarnessStatus()==cereal::PandaState::HarnessStatus::FLIPPED);CHECK(ps[0].getSafetyParam()==100);CHECK(ps[1].getSafetyParam()==101);});
      }
    });
    test("serialized_missing_and_extra_inventory_never_shrinks",[]{
      for(unsigned n: {0U,1U,3U}){Rig r;std::vector<SessionObservation> inv;
        if(n) { inv.push_back({r.profile.slots[1].expected,1}); }
        if(n==3){inv.push_back({r.profile.slots[0].expected,1});inv.push_back({r.profile.slots[0].expected,2});}
        CHECK(!r.runtime.reconcile(inv,{{&r.t0,&r.t1}}));r.runtime.tick();
        read_event(serialize_panda_states(r.runtime,r.clock.time),[](auto e){CHECK(!e.getValid());CHECK(e.getPandaStates().size()==2);CHECK(!e.getPandaStates()[0].getControlsAllowed());});
        trace_message(serialize_panda_states(r.runtime,r.clock.time),"pandaStates",0,r.clock.time,false);
        CHECK(!r.runtime.transmit_step(1));
      }
    });
    test("configuration_cardinality_and_offroad_barrier",[]{
      for(unsigned n:{0U,1U,3U}){Rig r;r.device();CHECK(!configure_cereal_params(r.runtime,params(n),r.runtime.capture()));CHECK(!r.runtime.snapshot().configured);}
      Rig r;r.device(true);CHECK(!configure_cereal_params(r.runtime,params(),r.runtime.capture()));
    });
    test("per_board_safety_writes_and_readbacks_are_not_replicated",[]{
      Rig r;r.ready();CHECK(r.b0.param==100);CHECK(r.b1.param==101);CHECK(r.b0.alternative==2);CHECK(r.b1.alternative==2);
      for(auto *b:{&r.b0,&r.b1}){bool set=false,read=false;for(const auto &c:b->calls){CHECK(c.request!=0xe7);CHECK(c.request!=0xe5);if(c.request==0xdc)set=true;if(c.request==0xd2&&set)read=true;}CHECK(set&&read);}
    });
    test("short_health_and_can_health_invalidate_whole_group",[]{
      for(unsigned request:{0xd2U,0xc2U}){Rig r;r.ready();r.engage();r.b0.bad_request=request;
        for(unsigned i=0;i<30&&r.runtime.sessions().slots[0].connected;++i){r.clock.time+=10000;r.runtime.tick();}
        CHECK(!r.runtime.snapshot().simulated_tx_allowed);CHECK(!r.runtime.snapshot().panda_states_valid);
        read_event(serialize_panda_states(r.runtime,r.clock.time),[](auto e){CHECK(!e.getValid());CHECK(e.getPandaStates().size()==2);});
      }
    });
    test("safety_readback_mismatch_cannot_enable",[]{
      Rig r;r.b1.mismatched=true;r.device();CHECK(configure_cereal_params(r.runtime,params(),r.runtime.capture()));
      for(unsigned i=0;i<12;++i) { r.runtime.tick(); }
      CHECK(!r.runtime.snapshot().configured);CHECK(!r.runtime.snapshot().simulated_tx_allowed);
    });
    test("fresh_disabled_to_enabled_edge_required",[]{
      Rig r;r.ready();++r.clock.time;CHECK(!r.input(state_event("selfdrive",r.clock.time,true)));CHECK(!r.runtime.snapshot().simulated_tx_allowed);
      // A rejected enable is a closed barrier: recover through fresh offroad configuration.
      r.ready();r.engage();++r.clock.time;CHECK(r.input(state_event("selfdrive",r.clock.time,false)));CHECK(!r.runtime.snapshot().simulated_tx_allowed);
    });
    test("offroad_transition_retires_epoch_queue_and_requires_onroad",[]{
      Rig r;r.ready();r.engage();++r.clock.time;CHECK(r.input(send_event(r.clock.time)));
      const auto old=r.runtime.capture();++r.clock.time;const auto offroad_time=r.clock.time;r.clock.time+=50;
      CHECK(ingest_cereal_event(r.runtime,state_event("device",offroad_time,false),old));
      CHECK(r.runtime.capture().control_epoch!=old.control_epoch);
      CHECK(!r.runtime.snapshot().simulated_tx_allowed);CHECK(r.runtime.snapshot().reason=="DEVICE_OFFROAD");
      CHECK(!r.runtime.transmit_step(1));CHECK(r.runtime.counters().simulated_tx==0);
      for(unsigned n=0;n<3;++n)r.runtime.tick();
      for(auto *board:{&r.b0,&r.b1}){CHECK(board->calls.back().request==0xf3);CHECK(board->calls.back().value==0);}
      // The accepted offroad Event remains usable even when received after its
      // original source time; no fabricated new timestamp is needed to configure.
      CHECK(configure_cereal_params(r.runtime,params(),r.runtime.capture()));
      for(unsigned n=0;n<16;++n){r.runtime.tick();++r.clock.time;}
      CHECK(r.runtime.snapshot().configured);for(unsigned n=0;n<3;++n)r.runtime.peripheral_step();
      ++r.clock.time;CHECK(r.input(state_event("selfdrive",r.clock.time,false)));
      ++r.clock.time;CHECK(!r.input(state_event("selfdrive",r.clock.time,true)));
      CHECK(!r.runtime.snapshot().simulated_tx_allowed);CHECK(r.runtime.snapshot().reason=="ONROAD_DEVICE_STATE_REQUIRED");
      r.ready();r.engage();CHECK(!r.runtime.transmit_step(1));CHECK(r.runtime.counters().simulated_tx==0);
    });
    test("selfdrive_future_invalid_and_frozen_enabled_close_gate",[]{
      for(unsigned mode=0;mode<4;++mode){Rig r;r.ready();r.engage();
        if(mode==0){r.clock.time+=100000;CHECK(!r.runtime.snapshot().simulated_tx_allowed);}
        else CHECK(!r.input(state_event("selfdrive",mode==3 ? 0 : r.clock.time+(mode==1?1:0),true,mode!=2)));
        CHECK(!r.runtime.transmit_step(1));CHECK(r.runtime.counters().simulated_tx==0);
      }
    });
    test("sendcan_validity_age_reserved_bus_and_original_epoch",[]{
      Rig r;r.ready();r.engage();const auto epoch=r.runtime.capture();
      CHECK(!r.input(send_event(r.clock.time,false)));CHECK(!r.input(send_event(r.clock.time,true,3)));
      CHECK(!r.input(send_event(r.clock.time+1)));++r.clock.time;CHECK(r.input(send_event(r.clock.time)));CHECK(r.runtime.transmit_step(1));
      r.runtime.invalid_input("INJECTED_FAULT");CHECK(!ingest_cereal_event(r.runtime,send_event(r.clock.time),epoch));CHECK(!r.runtime.transmit_step(1));
      CHECK(r.runtime.counters().simulated_tx==1);
    });
    test("fault_flushes_queued_can_and_old_cereal_input",[]{
      Rig r;r.ready();r.engage();++r.clock.time;const auto epoch=r.runtime.capture();CHECK(r.input(send_event(r.clock.time)));
      r.runtime.invalid_input("THERMAL_EXIT");const auto changed=r.runtime.capture();CHECK(changed.control_epoch!=epoch.control_epoch);
      CHECK(!ingest_cereal_event(r.runtime,{1,2,3},epoch));CHECK(r.runtime.capture().control_epoch==changed.control_epoch);
      r.ready();r.engage();CHECK(!r.runtime.transmit_step(1));CHECK(r.runtime.counters().simulated_tx==0);
    });
    test("transport_alias_and_cross_board_reuse_rejected",[]{
      {Rig r;CHECK(!r.runtime.reconcile({{r.profile.slots[0].expected,2},{r.profile.slots[1].expected,2}},{{&r.t1,&r.t0}}));}
      FakeClock c;auto p=fixture();FakeBoard b(c,true);Transport t(b,c);PandadRuntime rt(p,c);
      CHECK(!rt.reconcile({{p.slots[0].expected,1},{p.slots[1].expected,1}},{{&t,&t}}));CHECK(b.calls.empty());
    });
    test("reconnect_requires_new_handles_offroad_and_new_enable_edge",[]{
      Rig r;r.ready();r.engage();const auto old=r.runtime.capture();
      CHECK(!r.runtime.reconcile({{r.profile.slots[1].expected,1}},{{nullptr,&r.t1}}));
      FakeBoard replacement(r.clock,true);Transport fresh(replacement,r.clock);
      CHECK(r.runtime.reconcile({{r.profile.slots[0].expected,2},{r.profile.slots[1].expected,1}},{{&fresh,&r.t1}}));
      CHECK(!r.runtime.snapshot().simulated_tx_allowed);CHECK(!ingest_cereal_event(r.runtime,send_event(r.clock.time),old));
      r.ready();r.engage();++r.clock.time;CHECK(r.input(send_event(r.clock.time)));CHECK(r.runtime.transmit_step(1));
      r.runtime.shutdown();
    });
    test("six_bidirectional_routes_keep_local_wire_and_slot_identity",[]{
      Rig r;r.ready();r.engage();
      for(unsigned source:{0U,1U,2U,4U,5U,6U}){++r.clock.time;CHECK(r.input(send_event(r.clock.time,true,source)));CHECK(r.runtime.transmit_step(source/4));
        auto &board=source/4? r.b1:r.b0;const auto &wire=board.calls.back().payload;
        auto frame=decode_can_frame(wire.data(),wire.size(),CanCapability::CanFd,FrameDirection::Tx);CHECK(frame);CHECK(frame.value.local_bus==source%4);
        auto rx=encode_can_frame({0x321,source%4,false,false,false,false,{1,2,3}},CanCapability::CanFd,FrameDirection::Rx);CHECK(rx);
        board.can_bytes=rx.value;CHECK(r.runtime.receive_step(source/4));
        const auto serialized=serialize_can(r.runtime,r.clock.time);
        trace_message(serialized,"can",0,r.clock.time,true);
        read_event(serialized,[&](auto e){CHECK(e.getValid());CHECK(e.getCan().size()==1);CHECK(e.getCan()[0].getSrc()==source);});
      }
    });
    test("explicit_roles_aggregate_ignition_and_no_unsupported_ir",[]{
      for(unsigned dos=0;dos<2;++dos){Rig r(dos);r.ready();for(unsigned i=0;i<6;++i)r.runtime.peripheral_step();
        const auto p=r.runtime.peripheral_snapshot();CHECK(p.valid);CHECK(p.fan_slot==int(dos));CHECK(p.ignition&&*p.ignition);CHECK(p.harness&&*p.harness==2);CHECK(p.voltage&&*p.voltage==12001);CHECK(!p.current);CHECK(p.temperature&&*p.temperature==35);
        read_event(serialize_peripheral_state(r.runtime,r.clock.time),[](auto e){CHECK(e.getValid());CHECK(e.getPeripheralState().getFanSpeedRpm()==2345);CHECK(e.getPeripheralState().getCurrent()==0);});
        auto &external=dos==0?r.b1:r.b0;for(const auto &c:external.calls)CHECK(c.request!=0xb1&&c.request!=0xb2&&c.request!=0xb0);
        for(auto *b:{&r.b0,&r.b1})for(const auto &c:b->calls)CHECK(c.request!=0xe7&&c.request!=0xb0);
      }
      for(unsigned dos=0;dos<2;++dos){auto p=fixture(dos);const auto power=1-dos;
        p.slots[dos].expected.capabilities.power_monitor=false;p.slots[power].expected.capabilities.power_monitor=true;
        p.roles.power_monitor=power;Rig r(p);r.ready();const auto original=r.runtime.peripheral_snapshot();
        CHECK(original.voltage&&*original.voltage==13002);CHECK(original.current&&*original.current==654);
        CHECK(original.power_timestamp_us<original.timestamp_us);
        std::uint64_t published=0;
        read_event(serialize_peripheral_state(r.runtime,r.clock.time),[&](auto e){CHECK(e.getValid());const auto ps=e.getPeripheralState();
          CHECK(ps.getPandaType()==cereal::PandaState::PandaType::DOS);CHECK(ps.getVoltage()==13002);CHECK(ps.getCurrent()==654);
          CHECK(ps.getFanSpeedRpm()==2345);CHECK(e.getLogMonoTime()==original.power_timestamp_us*1000);published=e.getLogMonoTime();});
        r.clock.time+=1000;
        read_event(serialize_peripheral_state(r.runtime,r.clock.time),[&](auto e){CHECK(e.getValid());CHECK(e.getLogMonoTime()==published);});
      }
      {auto p=fixture();p.slots[0].expected.capabilities.power_monitor=false;p.roles.power_monitor=kRoleUnsupported;
        Rig r(p);r.ready();const auto observation=r.runtime.peripheral_snapshot();CHECK(!observation.valid);
        CHECK(observation.fan_rpm&&*observation.fan_rpm==2345);CHECK(!observation.voltage&&!observation.current);
        read_event(serialize_peripheral_state(r.runtime,r.clock.time),[](auto e){CHECK(!e.getValid());CHECK(e.getPeripheralState().getPandaType()==cereal::PandaState::PandaType::UNKNOWN);});
      }
    });
    test("thermal_staleness_preserves_explicit_exit_not_fabricated_fan",[]{
      Rig r;r.ready();r.engage();const auto n=r.b0.calls.size();r.clock.time+=750000;CHECK(!r.runtime.peripheral_step());CHECK(!r.runtime.snapshot().simulated_tx_allowed);
      for(unsigned i=0;i<6;++i) { r.runtime.tick(); }
      bool exit=false,hb=false;
      for(std::size_t i=n;i<r.b0.calls.size();++i){const auto &c=r.b0.calls[i];CHECK(c.request!=0xb1);if(c.request==0xdc&&c.value==19)exit=true;if(c.request==0xf3&&c.value==0)hb=true;}
      CHECK(exit&&hb);
    });
    test("missing_and_independently_stale_fan_observation_close_control",[]{
      { Rig r; r.device(); CHECK(configure_cereal_params(r.runtime,params(),r.runtime.capture()));
        for(unsigned i=0;i<16;++i) { r.runtime.tick(); ++r.clock.time; }
        CHECK(r.runtime.snapshot().configured);
        ++r.clock.time; CHECK(r.input(state_event("selfdrive",r.clock.time,false)));
        r.device(true); ++r.clock.time; CHECK(!r.input(state_event("selfdrive",r.clock.time,true)));
        CHECK(!r.runtime.snapshot().simulated_tx_allowed);
      }
      Rig r; r.ready(); r.engage();
      const auto fan_time=r.runtime.peripheral_snapshot().timestamp_us;
      for(unsigned i=0;i<24;++i) {
        r.clock.time=fan_time+10000*(i+1); r.runtime.tick();
        ++r.clock.time; CHECK(r.input(state_event("selfdrive",r.clock.time,true)));
        CHECK(r.runtime.snapshot().simulated_tx_allowed);
      }
      r.clock.time=fan_time+250000;
      const auto state=r.runtime.snapshot(); CHECK(!state.simulated_tx_allowed);
      CHECK(state.reason=="FAN_OBSERVABILITY_EXPIRED");
    });
    test("uncertain_tx_is_one_shot_and_never_routed_to_sibling",[]{
      Rig r;r.ready();r.engage();r.b1.uncertain_write=true;++r.clock.time;CHECK(r.input(send_event(r.clock.time)));
      CHECK(!r.runtime.transmit_step(1));CHECK(!r.runtime.transmit_step(1));CHECK(!r.runtime.transmit_step(0));
      unsigned n=0;for(const auto &c:r.b1.calls)n+=c.kind==Kind::BulkWrite;CHECK(n==1);
      for(const auto &c:r.b0.calls)CHECK(c.kind!=Kind::BulkWrite);
    });
    test("source_ages_survive_cereal_republication_and_exact_expiry",[]{
      Rig r;r.ready();std::uint64_t first=0;read_event(serialize_panda_states(r.runtime,r.clock.time),[&](auto e){first=e.getLogMonoTime();CHECK(e.getValid());});
      r.clock.time+=200000;read_event(serialize_panda_states(r.runtime,r.clock.time),[&](auto e){CHECK(e.getValid());CHECK(e.getLogMonoTime()==first);});
      r.clock.time+=50000;read_event(serialize_panda_states(r.runtime,r.clock.time),[](auto e){CHECK(!e.getValid());CHECK(e.getPandaStates().size()==2);});
    });
    test("malformed_cereal_and_oversized_inputs_fail_closed",[]{
      for(const auto &bytes:std::vector<Bytes>{{},Bytes{1},Bytes(65544),Bytes(16,255)}){Rig r;r.ready();CHECK(!r.input(bytes));CHECK(!r.runtime.snapshot().simulated_tx_allowed);}
    });
    test("actual_pandad_cycle_publishes_cereal_and_rejects_stalled_loop",[]{
      Rig r; r.ready();
      class EmptySource final : public EventSource {
      public: std::optional<InputEnvelope> next(InputEpoch) override { return {}; }
      } source;
      class Sink final : public EventSink {
      public:
        unsigned messages=0;
        bool publish(OutputTopic topic, const MessageBytes &bytes) override {
          ++messages; read_event(bytes,[&](auto e){
            CHECK((topic==OutputTopic::Can && e.which()==cereal::Event::CAN) ||
                  (topic==OutputTopic::PandaStates && e.which()==cereal::Event::PANDA_STATES) ||
                  (topic==OutputTopic::PeripheralState && e.which()==cereal::Event::PERIPHERAL_STATE));
          }); return true;
        }
      } sink;
      PandadLoop loop(r.runtime,r.clock,source,sink);
      const auto first=loop.cycle(); CHECK(first.publications==3); CHECK(!first.callback_failed&&!first.budget_exceeded);
      r.clock.time+=10000; const auto second=loop.cycle(); CHECK(second.publications==1); CHECK(sink.messages==4);
      r.clock.time+=250000; const auto overdue=loop.cycle(); CHECK(overdue.overdue); CHECK(!r.runtime.snapshot().simulated_tx_allowed);
    });
    test("planned_shutdown_has_bounded_exit_and_cancels_all_handles",[]{
      Rig r;r.ready();r.engage();r.runtime.shutdown();CHECK(!r.t0.healthy()&&!r.t1.healthy());CHECK(!r.runtime.snapshot().simulated_tx_allowed);
      const auto n=r.b0.calls.size()+r.b1.calls.size();r.runtime.shutdown();r.runtime.tick();CHECK(n==r.b0.calls.size()+r.b1.calls.size());
      for(auto *b:{&r.b0,&r.b1}){CHECK(b->calls.back().request==0xf3);CHECK(b->calls.back().value==0);}
    });
    std::cout<<"{\"suite\":\"pandad_runtime_cereal\",\"passed\":"<<passed.size()<<",\"failed\":0,\"skipped\":0,\"assertions\":"<<assertions
      <<",\"device_access\":false,\"real_can_tx\":false,\"schema_changed\":false,\"backend\":\"P2_fake_only\"}\n";
    return 0;
  } catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}
}
}  // namespace openpilot::pandad::compatibility
