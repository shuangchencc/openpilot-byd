#include "../pandad_loop.h"

#include <algorithm>
#include <atomic>
#include <array>
#include <cstring>
#include <deque>
#include <functional>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#include <capnp/message.h>
#include <capnp/serialize.h>
#include "openpilot/cereal/gen/cpp/log.capnp.h"

using namespace openpilot::pandad::compatibility;
namespace {
std::size_t assertions = 0;
void check(bool ok, int line) {
  ++assertions;
  if (!ok) throw std::runtime_error("P6 fault assertion line " + std::to_string(line));
}
#define CHECK(x) check(static_cast<bool>(x), __LINE__)
using Bytes = MessageBytes;
void put16(Bytes &b, unsigned n, unsigned value) { b[n] = value; b[n + 1] = value >> 8; }
void put32(Bytes &b, unsigned n, unsigned value) { put16(b,n,value); put16(b,n+2,value>>16); }
SessionProfile profile() {
  SessionProfile p; p.binding_status = "SYNTHETIC_BOUND";
  for (unsigned s = 0; s < 2; ++s) {
    p.slots[s].expected = {s ? "fixture:red" : "fixture:dos", s ? "RED_PANDA" : "DOS", s ? "H7" : "F4",
      s ? "fixture:H7" : "fixture:F4", "USB", kSessionProtocol,
      {{0,1,2},s==1,s==1,s==1,true,s==0,s==0,false}, "fixture:app"};
    p.slots[s].safety_config_semantics = "noOutput";
  }
  p.roles = {1,1,1,{0,1},0,0,kRoleUnsupported};
  return p;
}
struct FakeClock final : Clock {
  std::uint64_t now = 1000000;
  std::uint64_t now_us() override { return now; }
  void wait_us(std::uint32_t amount) override { now += amount; }
};
struct Call { unsigned slot; Request request; std::uint64_t time; };
struct Board final : Backend {
  FakeClock &clock; unsigned slot;
  std::vector<Call> &trace;
  unsigned param = 0, alternative = 0, cancellations = 0, model = kNoOutputSafetyModel;
  std::uint32_t faults = 0, blocked = 0, fwd_blocked = 0;
  std::deque<Result> reads;
  std::function<std::optional<Result>(const Request &)> inject;
  Board(FakeClock &c, unsigned s, std::vector<Call> &t) : clock(c), slot(s), trace(t) {}
  Result transact(const Request &r, std::uint32_t timeout) override {
    CHECK(timeout > 0 && timeout <= kRuntimeIoBudgetMs);
    trace.push_back({slot,r,clock.now});
    if (inject) if (auto result = inject(r)) return *result;
    if (r.kind == Kind::ControlWrite) {
      CHECK(r.request==0xdc || r.request==0xdf || r.request==0xf3 || r.request==0xb1);
      if (r.request==0xdc) { CHECK(r.value==kNoOutputSafetyModel); param=r.index; model=r.value; }
      if (r.request==0xdf) alternative=r.value;
      return {Status::Ok,0,{}};
    }
    if (r.kind==Kind::BulkWrite) return {Status::Ok,r.payload.size(),{}};
    if (r.kind==Kind::BulkRead) {
      if (reads.empty()) return {Status::Short,0,{}};
      auto result=std::move(reads.front()); reads.pop_front(); return result;
    }
    Bytes b(r.read_length);
    if (r.request==0xd5) {
      CHECK(slot==0); CHECK(r.read_length==16);
      b={'F','4','C','1',1,0,16,0,31,0,0,0,7,0,0,0};
    } else if (r.request==0xd2) {
      put16(b,4,12000); put16(b,6,slot==0 ? 0 : 420); put32(b,8,blocked); put32(b,24,faults);
      b[28]=1; b[30]=2; b[31]=model; put16(b,32,param); put16(b,35,alternative); b[47]=75;
    } else if (r.request==0xc2) {
      put32(b,37,fwd_blocked); put16(b,41,500); put16(b,43,slot==0 ? 0 : 2000);
    } else if (r.request==0xb2) put16(b,0,1200);
    else throw std::runtime_error("unexpected control request " + std::to_string(r.request));
    return {Status::Ok,b.size(),b};
  }
  void cancel() noexcept override { ++cancellations; }
  void rx(Bytes b) { reads.push_back({Status::Short,b.size(),std::move(b)}); }
};
Bytes frame(unsigned bus=0, bool returned=false, bool rejected=false, unsigned data=1, bool fd=false) {
  CanFrame f{0x123,bus,false,fd,returned,rejected,Bytes(fd ? 12 : 1,static_cast<std::uint8_t>(data))};
  auto out=encode_can_frame(f,CanCapability::CanFd,FrameDirection::Rx); CHECK(out); return out.value;
}
Bytes flatten(capnp::MallocMessageBuilder &b) {
  const auto words=capnp::messageToFlatArray(b); const auto bytes=words.asBytes(); return {bytes.begin(),bytes.end()};
}
Bytes send_event(std::uint64_t now, unsigned source) {
  capnp::MallocMessageBuilder b; auto e=b.initRoot<cereal::Event>(); e.setValid(true); e.setLogMonoTime(now*1000);
  auto f=e.initSendcan(1)[0]; f.setAddress(0x123); f.setSrc(source); const std::uint8_t data[]{1};
  f.setDat(kj::arrayPtr(data,1)); return flatten(b);
}
struct Source final : EventSource {
  std::deque<InputEnvelope> queued; unsigned attempts=0;
  std::optional<InputEnvelope> next(InputEpoch) override {
    ++attempts; if (queued.empty()) return std::nullopt;
    auto out=std::move(queued.front()); queued.pop_front(); return out;
  }
};
struct Sink final : EventSink {
  std::vector<std::pair<OutputTopic,Bytes>> records;
  bool publish(OutputTopic t,const Bytes &b) override { records.emplace_back(t,b); return true; }
};
struct Rig {
  FakeClock clock; SessionProfile p=profile(); std::vector<Call> trace;
  std::vector<std::unique_ptr<Board>> boards_store;
  std::vector<std::unique_ptr<Transport>> transports_store;
  std::array<Board *,2> boards{}; std::array<Transport *,2> transports{};
  std::array<std::uint64_t,2> tokens{{1,1}};
  PandadRuntime runtime{p,clock}; Source source; Sink sink; PandadLoop loop{runtime,clock,source,sink};
  Rig() { attach(0); attach(1); CHECK(reconcile()); }
  void attach(unsigned s) {
    boards_store.push_back(std::make_unique<Board>(clock,s,trace)); boards[s]=boards_store.back().get();
    transports_store.push_back(std::make_unique<Transport>(*boards[s],clock)); transports[s]=transports_store.back().get();
  }
  bool reconcile() {
    return runtime.reconcile({{p.slots[0].expected,tokens[0]},{p.slots[1].expected,tokens[1]}},transports);
  }
  void ready() {
    ++clock.now; CHECK(runtime.device_input({true,false,true,25,clock.now},runtime.capture()));
    CHECK(runtime.configure({{0,19,11,0},{1,19,22,0}},runtime.capture()));
    for (unsigned i=0;i<20;++i) { runtime.tick(); clock.now+=1000; }
    CHECK(runtime.snapshot().panda_states_valid); CHECK(!runtime.snapshot().simulated_tx_allowed);
    for (unsigned i=0;i<3;++i) runtime.peripheral_step();
  }
  void engage() {
    ++clock.now; CHECK(runtime.selfdrive(true,false,clock.now,runtime.capture()));
    ++clock.now; CHECK(runtime.device_input({true,true,true,25,clock.now},runtime.capture()));
    ++clock.now; CHECK(runtime.selfdrive(true,true,clock.now,runtime.capture()));
    CHECK(runtime.snapshot().simulated_tx_allowed); CHECK(!runtime.snapshot().real_tx_authorized);
  }
  TxBatch batch(std::initializer_list<unsigned> sources) {
    TxBatch b; b.valid=true; b.source_timestamp_us=++clock.now;
    for (const auto source:sources) b.frames.push_back({0x123,source,false,false,{1}});
    return b;
  }
  bool queue(std::initializer_list<unsigned> sources) { return runtime.sendcan(batch(sources),runtime.capture()); }
  std::size_t writes(unsigned slot) const {
    return std::count_if(trace.begin(),trace.end(),[&](const Call &c){ return c.slot==slot && c.request.kind==Kind::BulkWrite; });
  }
  std::size_t total_writes() const { return writes(0)+writes(1); }
  void positive() {
    ready(); engage(); CHECK(queue({0,4})); CHECK(runtime.transmit_step(0)); CHECK(runtime.transmit_step(1));
    CHECK(writes(0)==1 && writes(1)==1); CHECK(runtime.counters().simulated_tx==2);
  }
  void closed() {
    CHECK(!runtime.snapshot().simulated_tx_allowed); CHECK(!runtime.snapshot().real_tx_authorized);
    const auto before=total_writes(); CHECK(!runtime.transmit_step(0)); CHECK(!runtime.transmit_step(1)); CHECK(total_writes()==before);
  }
  void no_bypass(std::size_t start) const {
    for (std::size_t i=start;i<trace.size();++i) {
      const auto &q=trace[i].request;
      if (q.kind==Kind::ControlWrite) {
        CHECK(q.request==0xdc || q.request==0xdf || q.request==0xf3 || q.request==0xb1);
        if (q.request==0xdc) CHECK(q.value==kNoOutputSafetyModel);
        if (q.request==0xdf) CHECK(q.value==0);
      }
    }
  }
};
void inspect(const Bytes &bytes,const std::function<void(cereal::Event::Reader)> &fn) {
  auto words=kj::heapArray<capnp::word>(bytes.size()/8); std::memcpy(words.begin(),bytes.data(),bytes.size());
  capnp::FlatArrayMessageReader reader(words); fn(reader.getRoot<cereal::Event>());
}
void assert_exit(const Rig &r,unsigned survivor,std::size_t start) {
  unsigned phase=0;
  for (std::size_t i=start;i<r.trace.size();++i) {
    const auto &c=r.trace[i]; if (c.slot!=survivor) continue;
    const auto &q=c.request;
    CHECK(q.kind!=Kind::BulkWrite && q.kind!=Kind::BulkRead);
    if (q.request==0xdc) { CHECK(q.value==19 && q.index==0); phase=1; }
    else if (q.request==0xd2 && phase==1) phase=2;
    else if (q.request==0xf3 && phase==2) { CHECK(q.value==0); phase=3; }
  }
  CHECK(phase==3);
}
}
int main() {
  std::vector<std::string> passed;
  const auto test=[&](const char *name,const std::function<void()> &fn) { fn(); passed.push_back(name); std::cout<<"PASS "<<name<<'\n'; };
  try {
    test("p6_positive_dual_route_all_local_buses_and_returned_rx_provenance",[]{
      Rig r; r.positive();
      for (unsigned bus=0;bus<3;++bus) {
        CHECK(r.queue({bus,4+bus})); CHECK(r.runtime.transmit_step(0)); CHECK(r.runtime.transmit_step(1));
        r.boards[0]->rx(frame(bus,true)); r.boards[1]->rx(frame(bus,true));
        CHECK(r.runtime.receive_step(0)); CHECK(r.runtime.receive_step(1));
        const auto rx=r.runtime.receive_frames(); CHECK(rx.size()==2);
        CHECK(rx[0].source==128+bus && rx[1].source==132+bus);
        CHECK(rx[0].frame.returned && rx[1].frame.returned); CHECK(!rx[0].frame.rejected && !rx[1].frame.rejected);
      }
      CHECK(r.writes(0)==4 && r.writes(1)==4); CHECK(r.runtime.snapshot().simulated_tx_allowed);
      CHECK(!r.p.device_access_enabled && !r.p.can_tx_enabled && !r.p.firmware_install_enabled);
    });
    test("p6_disconnect_reconnect_drops_both_queues_and_requires_fresh_edges",[]{
      for (unsigned lost=0;lost<2;++lost) {
        Rig r; r.positive(); CHECK(r.queue({0,4})); const auto stale=r.runtime.capture();
        r.boards[lost]->reads.push_back({Status::Disconnected}); CHECK(!r.runtime.receive_step(lost)); r.closed();
        CHECK(r.runtime.sessions().slots[1-lost].connected); CHECK(!r.runtime.sessions().slots[lost].connected);
        ++r.tokens[lost]; r.attach(lost); CHECK(r.reconcile());
        CHECK(!r.runtime.selfdrive(true,true,++r.clock.now,r.runtime.capture())); r.closed();
        r.ready();
        CHECK(!r.runtime.selfdrive(true,true,++r.clock.now,r.runtime.capture()));
        // The bad enable intentionally closes the group; a new offroad barrier is required.
        r.ready(); r.engage();
        const auto epoch=r.runtime.capture(); CHECK(!ingest_cereal_event(r.runtime,{1,2,3},stale));
        CHECK(r.runtime.capture().control_epoch==epoch.control_epoch); CHECK(r.runtime.snapshot().simulated_tx_allowed);
        CHECK(!r.runtime.transmit_step(0) && !r.runtime.transmit_step(1)); CHECK(r.total_writes()==2);
        CHECK(r.queue({0,4})); CHECK(r.runtime.transmit_step(0)); CHECK(r.runtime.transmit_step(1)); CHECK(r.total_writes()==4);
      }
    });
    test("p6_direct_handle_replacement_retires_fragments_and_old_envelopes",[]{
      Rig r; r.positive(); const auto wire=frame(); r.boards[0]->rx(Bytes(wire.begin(),wire.begin()+3));
      CHECK(r.runtime.receive_step(0)); CHECK(r.queue({0,4}));
      const auto old=r.runtime.capture(); Board *retired=r.boards[0]; ++r.tokens[0]; r.attach(0); CHECK(r.reconcile());
      CHECK(retired->cancellations>0); r.closed(); r.ready(); r.engage();
      CHECK(!r.runtime.sendcan(r.batch({4}),old)); CHECK(r.runtime.snapshot().simulated_tx_allowed);
      r.boards[0]->rx(wire); CHECK(r.runtime.receive_step(0)); CHECK(r.runtime.receive_frames().size()==1);
      CHECK(!r.runtime.transmit_step(0) && !r.runtime.transmit_step(1)); CHECK(r.total_writes()==2);
    });
    test("p6_unknown_replacement_cannot_inherit_role_or_profile",[]{
      Rig r; r.positive(); CHECK(r.queue({0,4})); auto wrong=r.p.slots[1].expected;
      wrong.identity="fixture:replacement";
      CHECK(!r.runtime.reconcile({{r.p.slots[0].expected,1},{wrong,2}},r.transports)); r.closed();
      CHECK(r.runtime.profile().slots[1].expected.identity=="fixture:red"); CHECK(r.total_writes()==2);
      r.tokens[1]=3; r.attach(1); CHECK(r.reconcile()); r.ready(); r.engage();
      CHECK(!r.runtime.transmit_step(1)); CHECK(r.queue({4})); CHECK(r.runtime.transmit_step(1));
    });
    test("p6_expired_tx_queue_never_replayed_on_other_board",[]{
      for (const auto age:{kTxQueueMaxAgeUs-1,kTxQueueMaxAgeUs,kTxQueueMaxAgeUs+1}) {
        Rig r; r.positive(); CHECK(r.queue({0,4})); r.clock.now+=age;
        CHECK(r.runtime.transmit_step(0)==(age<kTxQueueMaxAgeUs));
        CHECK(r.runtime.transmit_step(1)==(age<kTxQueueMaxAgeUs));
        CHECK(r.total_writes()==(age<kTxQueueMaxAgeUs ? 4U : 2U));
        CHECK(r.runtime.snapshot().simulated_tx_allowed);
      }
    });
    test("p6_expired_partial_frame_closes_before_suffix_or_queued_tx",[]{
      Rig r; r.positive(); const auto wire=frame(); r.boards[0]->rx(Bytes(wire.begin(),wire.begin()+3));
      CHECK(r.runtime.receive_step(0)); const auto fragment_at=r.clock.now; CHECK(r.queue({4}));
      r.clock.now=fragment_at+kRxMaxAgeUs-3; CHECK(r.runtime.selfdrive(true,true,r.clock.now,r.runtime.capture()));
      r.clock.now=fragment_at+kRxMaxAgeUs; r.boards[0]->rx(Bytes(wire.begin()+3,wire.end()));
      const auto before=r.trace.size(); CHECK(!r.runtime.receive_step(0)); CHECK(r.trace.size()==before); r.closed();
      CHECK(r.runtime.receive_frames().empty());
    });
    test("p6_bad_checksum_disconnect_and_stale_health_remain_closed",[]{
      Rig r; r.positive(); CHECK(r.queue({0,4})); auto bad=frame(); bad.back()^=0x80;
      r.boards[0]->rx(bad); r.boards[1]->reads.push_back({Status::Disconnected});
      CHECK(!r.runtime.receive_step(0)); const auto after_bad=r.trace.size(); CHECK(!r.runtime.receive_step(1));
      CHECK(r.trace.size()==after_bad); r.clock.now+=kHealthMaxAgeUs; r.closed();
      CHECK(!r.runtime.snapshot().panda_states_valid); CHECK(r.runtime.receive_frames().empty()); CHECK(r.total_writes()==2);
      CHECK(!r.reconcile());  // the same handles/tokens cannot heal corruption
      r.tokens={2,2}; r.attach(0); r.attach(1); CHECK(r.reconcile()); r.ready(); r.engage();
      CHECK(r.queue({4})); CHECK(r.runtime.transmit_step(1)); CHECK(r.total_writes()==3);
    });
    test("p6_tx_and_rx_queue_overflow_close_both_slots_before_dispatch",[]{
      for (unsigned rx=0;rx<2;++rx) {
        Rig r; r.positive();
        if (rx) {
          CHECK(r.queue({0,4})); Bytes stream; const auto wire=frame();
          for (unsigned i=0;i<=kRxFramesPerSlot;++i) stream.insert(stream.end(),wire.begin(),wire.end());
          r.boards[0]->rx(stream); CHECK(!r.runtime.receive_step(0));
        } else {
          for (unsigned slot=0;slot<2;++slot) {
            auto full=r.batch({slot*4}); full.frames.resize(kTxFramesPerSlot,full.frames.front());
            CHECK(r.runtime.sendcan(full,r.runtime.capture()));
          }
          CHECK(!r.queue({0}));
        }
        r.closed(); CHECK(r.total_writes()==2); CHECK(r.runtime.receive_frames().empty());
      }
    });
    test("p6_stale_cereal_backlog_cannot_poison_or_enable_reconnected_group",[]{
      Rig r; r.positive(); const auto old=r.runtime.capture();
      r.source.queued.push_back({InputKind::Event,send_event(r.clock.now,4),old});
      r.source.queued.push_back({InputKind::Event,{1,2,3},old});
      ++r.tokens[1]; r.attach(1); CHECK(r.reconcile()); r.ready(); r.engage();
      const auto current=r.runtime.capture(); const auto result=r.loop.cycle();
      CHECK(result.inputs==2 && result.rejected==2 && result.accepted==0);
      CHECK(r.runtime.capture().control_epoch==current.control_epoch); CHECK(r.runtime.snapshot().simulated_tx_allowed);
      CHECK(r.total_writes()==2);
    });
    test("p6_uncertain_partial_write_not_retried_rerouted_or_diagnostic",[]{
      for (unsigned failed=0;failed<2;++failed) {
        Rig r; r.positive(); CHECK(r.queue({0,4})); const auto start=r.trace.size();
        r.boards[failed]->inject=[](const Request &q)->std::optional<Result> {
          if (q.kind==Kind::BulkWrite) return Result{Status::Timeout,2,{},false,true};
          return std::nullopt;
        };
        CHECK(!r.runtime.transmit_step(failed)); r.closed();
        for (unsigned i=0;i<6;++i) { r.runtime.tick(); CHECK(!r.runtime.transmit_step(1-failed)); }
        CHECK(r.writes(failed)==2 && r.writes(1-failed)==1); r.no_bypass(start);
      }
    });
    test("p6_mcu_rejection_closes_before_same_or_sibling_queued_tx",[]{
      for (unsigned rejected=0;rejected<2;++rejected) {
        Rig r; r.positive(); CHECK(r.queue({0,4})); const auto start=r.trace.size();
        // Golden bytes independently emitted by the P6 actual-source F4/H7
        // safety oracle: noOutput rejected address 0x321, bus1, eight zeros.
        const auto original=r.runtime.sessions().slots[rejected];
        r.boards[rejected]->rx({0x82,0x09,0x19,0,0,0x92,0,0,0,0,0,0,0,0});
        CHECK(!r.runtime.receive_step(rejected)); r.closed();
        CHECK(r.runtime.rejection_count()==1); const auto evidence=r.runtime.rejection_evidence(); CHECK(evidence);
        CHECK(evidence->token.slot==rejected && evidence->token.epoch==original.epoch);
        CHECK(evidence->token.connection_token==original.connection_token);
        CHECK(evidence->local_bus==1 && evidence->source==193+4*rejected && evidence->address==0x321);
        CHECK(!r.queue({4*(1-rejected)})); CHECK(r.runtime.receive_frames().empty());
        for (unsigned i=0;i<6;++i) r.runtime.tick();
        CHECK(r.total_writes()==2); r.no_bypass(start);
      }
    });
    test("p6_relay_fault_and_forward_block_counter_close_queued_tx",[]{
      for (unsigned fault=0;fault<2;++fault) {
        Rig r; r.positive();
        if (fault==0) r.boards[1]->faults=1; else r.boards[1]->fwd_blocked=1;
        r.clock.now+=kHealthPeriodUs/2;
        CHECK(r.runtime.selfdrive(true,true,r.clock.now,r.runtime.capture()));
        r.clock.now+=kHealthPeriodUs/2;
        CHECK(r.runtime.selfdrive(true,true,r.clock.now,r.runtime.capture()));
        CHECK(r.queue({0,4}));
        for (unsigned i=0;i<10 && r.runtime.snapshot().simulated_tx_allowed;++i) r.runtime.tick();
        r.closed(); CHECK(r.total_writes()==2);
      }
    });
    test("p6_safety_readback_mismatch_never_changes_mode_or_reroutes",[]{
      Rig r; r.positive(); r.boards[1]->model=35;
      r.clock.now+=kHealthPeriodUs/2; CHECK(r.runtime.selfdrive(true,true,r.clock.now,r.runtime.capture()));
      r.clock.now+=kHealthPeriodUs/2; CHECK(r.runtime.selfdrive(true,true,r.clock.now,r.runtime.capture()));
      CHECK(r.queue({0,4})); const auto start=r.trace.size();
      for (unsigned i=0;i<6 && r.runtime.snapshot().simulated_tx_allowed;++i) r.runtime.tick();
      r.closed(); CHECK(r.total_writes()==2);
      for (unsigned i=0;i<5;++i) { r.runtime.tick(); } r.no_bypass(start);
    });
    test("p6_nooutput_only_policy_rejects_alloutput_and_silent_as_configuration",[]{
      for (const auto mode:{0U,17U,35U,32U}) {
        Rig r; r.ready(); ++r.clock.now;
        CHECK(r.runtime.device_input({true,false,true,25,r.clock.now},r.runtime.capture()));
        CHECK(!r.runtime.configure({{0,mode,33,0},{1,19,22,0}},r.runtime.capture())); r.closed();
        for (unsigned i=0;i<5;++i) { r.runtime.tick(); } r.no_bypass(0); CHECK(r.total_writes()==0);
      }
    });
    test("p6_planned_shutdown_exits_both_and_discards_queued_commands",[]{
      Rig r; r.positive(); CHECK(r.queue({0,4})); const auto start=r.trace.size(); r.runtime.shutdown();
      r.closed(); assert_exit(r,0,start); assert_exit(r,1,start); CHECK(r.total_writes()==2);
      CHECK(r.trace.size()-start<=10); CHECK(!r.transports[0]->healthy() && !r.transports[1]->healthy());
      const auto end=r.trace.size(); r.runtime.shutdown(); r.runtime.tick(); CHECK(r.trace.size()==end); CHECK(!r.reconcile());
    });
    test("p6_shutdown_blocked_or_cancelled_board_cannot_starve_sibling_exit",[]{
      for (unsigned failed=0;failed<2;++failed) for (unsigned mode=0;mode<2;++mode) {
        Rig r; r.positive(); CHECK(r.queue({0,4})); const auto start=r.trace.size(); const auto started=r.clock.now;
        if (mode==0) r.boards[failed]->inject=[&](const Request &)->std::optional<Result> {
          r.clock.now+=5000; return Result{Status::Timeout,0,{},false,true};
        }; else r.transports[failed]->cancel();
        r.runtime.shutdown(); r.closed(); assert_exit(r,1-failed,start); CHECK(r.total_writes()==2);
        CHECK(r.trace.size()-start<=10); CHECK(r.clock.now-started<=50000); r.no_bypass(start);
      }
    });
    test("p6_shutdown_transport_lock_contention_cancels_without_blocking_sibling",[]{
      for (unsigned held=0;held<2;++held) {
        Rig r; r.positive(); CHECK(r.queue({0,4}));
        std::atomic<bool> entered{false},release{false}; Result background;
        r.boards[held]->inject=[&](const Request &q)->std::optional<Result> {
          if (q.kind==Kind::ControlRead && q.request==0xd2) {
            entered.store(true);
            while (!release.load()) std::this_thread::yield();
            return Result{Status::Ok,48,Bytes(48)};
          }
          return std::nullopt;
        };
        Request request; request.kind=Kind::ControlRead; request.request=0xd2; request.read_length=48;
        std::thread pending([&]{ background=r.transports[held]->perform(request,5); });
        struct Join {
          std::atomic<bool> &release; std::thread &thread;
          ~Join() { release.store(true); if (thread.joinable()) thread.join(); }
        } guard{release,pending};
        while (!entered.load()) std::this_thread::yield();
        const auto start=r.trace.size(); const auto started=r.clock.now;
        r.runtime.shutdown();
        release.store(true); pending.join();
        CHECK(background.status==Status::Cancelled); CHECK(r.clock.now-started<=50000);
        assert_exit(r,1-held,start); r.closed(); CHECK(r.total_writes()==2);
        CHECK(!r.transports[held]->healthy()); CHECK(r.trace.size()-start<=10);
      }
    });
    test("p6_simulated_sigint_owner_handoff_precedes_backlog_and_publishes_invalid",[]{
      Rig r; r.positive(); CHECK(r.queue({0,4}));
      r.source.queued.push_back({InputKind::Event,send_event(r.clock.now,4),r.runtime.capture()});
      const auto start=r.trace.size(); r.loop.request_shutdown(); const auto result=r.loop.cycle();
      CHECK(result.shutdown); CHECK(result.inputs==0 && result.receive_attempts==0 && result.transmit_attempts==0);
      CHECK(r.source.attempts==0 && r.source.queued.size()==1); CHECK(result.publications==3);
      assert_exit(r,0,start); assert_exit(r,1,start); r.closed(); CHECK(r.total_writes()==2);
      for (const auto &record:r.sink.records) inspect(record.second,[](auto e){ CHECK(!e.getValid()); });
      const auto end=r.trace.size(); CHECK(r.loop.cycle().shutdown); CHECK(r.trace.size()==end);
    });
    test("p6_abrupt_process_loss_has_no_host_finally_or_exit_writes",[]{
      auto r=std::make_unique<Rig>(); r->positive(); CHECK(r->queue({0,4}));
      const auto before=r->trace.size(); const auto hb=std::count_if(r->trace.begin(),r->trace.end(),[](const Call &c){return c.request.request==0xf3;});
      // The model stops scheduling. It deliberately does NOT call shutdown,
      // tick, or invent a callback from a dead host. MCU fallback is verified by
      // the separate actual-source firmware/safety oracle, not this fixture.
      r->clock.now+=10000000; CHECK(r->trace.size()==before);
      CHECK(std::count_if(r->trace.begin(),r->trace.end(),[](const Call &c){return c.request.request==0xf3;})==hb);
      CHECK(r->total_writes()==2);
      std::cout<<"TRACE abrupt_process_loss host_calls_after_stop=0 shutdown_executed=false firmware_fallback_claimed=false\n";
      r.reset();
    });
    test("p6_simultaneous_power_loss_keeps_survivor_unpromoted_and_restart_closed",[]{
      Rig r; r.positive(); CHECK(r.queue({0,4})); const auto before=r.trace.size();
      // Model removal of power from both boards as complete empty inventory.
      CHECK(!r.runtime.reconcile({},{{nullptr,nullptr}})); r.closed();
      for (unsigned i=0;i<5;++i) { r.runtime.tick(); } CHECK(r.trace.size()==before); CHECK(r.total_writes()==2);
      r.tokens={2,2}; r.attach(0); r.attach(1); CHECK(r.reconcile()); r.closed();
      CHECK(!r.runtime.selfdrive(true,true,++r.clock.now,r.runtime.capture())); r.closed();
      r.ready(); r.engage(); CHECK(!r.runtime.transmit_step(0) && !r.runtime.transmit_step(1));
      CHECK(r.queue({4})); CHECK(r.runtime.transmit_step(1));
      std::cout<<"TRACE simultaneous_power_loss board_outputs_unobserved=true recovery_requires_fresh_configuration=true\n";
    });
    test("p6_loop_receive_fault_stops_sibling_transmit_in_same_cycle",[]{
      Rig r; r.positive(); CHECK(r.queue({0,4})); auto bad=frame(); bad.back()^=0x40; r.boards[0]->rx(bad);
      const auto result=r.loop.cycle(); CHECK(result.receive_attempts<=2 && result.transmit_attempts<=2);
      CHECK(r.total_writes()==2); r.closed();
      for (const auto &record:r.sink.records) if (record.first==OutputTopic::Can)
        inspect(record.second,[](auto e){ CHECK(!e.getValid()); CHECK(e.getCan().size()==0); });
    });
    test("p6_bounded_timeout_does_not_drain_queue_or_repeat_write",[]{
      Rig r; r.positive(); CHECK(r.queue({0,4})); const auto start=r.trace.size();
      r.boards[0]->inject=[&](const Request &q)->std::optional<Result> {
        if (q.kind==Kind::BulkRead) { r.clock.now+=5000; return Result{Status::Timeout,0,{}}; }
        return std::nullopt;
      };
      const auto result=r.loop.cycle();
      CHECK(result.elapsed_us<=10000); CHECK(result.receive_attempts<=2 && result.transmit_attempts<=2);
      unsigned reads=0; for (std::size_t i=start;i<r.trace.size();++i) reads+=r.trace[i].slot==0 && r.trace[i].request.kind==Kind::BulkRead;
      CHECK(reads==1); CHECK(r.writes(0)==2 && r.writes(1)==2); CHECK(r.runtime.snapshot().simulated_tx_allowed);
    });
    std::cout<<"{\"suite\":\"p6_compound_faults\",\"passed\":"<<passed.size()<<",\"failed\":0,\"skipped\":0,\"assertions\":"<<assertions
      <<",\"device_access\":false,\"real_can_tx\":false,\"schema_changed\":false,\"abrupt_host_finally_claimed\":false}\n";
    return 0;
  } catch (const std::exception &e) { std::cerr<<e.what()<<'\n'; return 1; }
}
