#include "../discovery_scheduler.h"
#include <algorithm>
#include <functional>
#include <iostream>
#include <stdexcept>

using namespace openpilot::pandad::compatibility;
namespace {
unsigned assertions=0;
#define CHECK(x) do { ++assertions; if (!(x)) throw std::runtime_error(std::string(__func__)+":"+std::to_string(__LINE__)+": " #x); } while(false)
struct FakeClock final:Clock {
  std::uint64_t value=1000000; unsigned reads=0, waits=0; bool throws=false;
  std::uint64_t now_us() override {++reads;if(throws)throw std::runtime_error("clock");return value;}
  void wait_us(std::uint32_t) override {++waits;throw std::runtime_error("scheduler waited");}
};
SessionProfile profile(bool native=false) {
  SessionProfile p; p.binding_status=native ? "NATIVE_READ_ONLY_BOUND":"SYNTHETIC_BOUND";
  if(native){p.schema=kNativeSessionProfileSchema;p.purpose="native_read_only_candidate";}
  for(unsigned s=0;s<2;++s) {
    p.slots[s].expected={native ? "uid:"+std::string(24,'a'+s):(s?"fixture:red":"fixture:dos"),
      s?"RED_PANDA":"DOS",s?"H7":"F4", native ? (s?"STM32H725IGK6":"STM32F413VGT6"):(s?"fixture:H7":"fixture:F4"),
      s?"SPI":"USB",kSessionProtocol,{{0,1,2},s==1,s==1,s==1,true,s==0,s==0,false},native?std::string(256,'a'+s):"fixture:app"};
    p.slots[s].safety_config_semantics="noOutput";
  }
  p.roles={1,1,1,{0,1},0,0,kRoleUnsupported};return p;
}
DiscoveryScheduleConfig enabled(){DiscoveryScheduleConfig c;c.allow_scheduling=true;return c;}
struct FakeJobState {
  unsigned polls=0,cancels=0,destroys=0; bool throws=false;
  std::uint64_t poll_cost=0;
  DiscoveryPollResult next;
};
struct FakeJob final:DiscoveryTask {
  std::shared_ptr<FakeJobState> state; FakeClock &clock;
  FakeJob(std::shared_ptr<FakeJobState> s,FakeClock &c):state(std::move(s)),clock(c){}
  ~FakeJob() override {++state->destroys;}
  DiscoveryPollResult poll() override {++state->polls;clock.value+=state->poll_cost;if(state->throws)throw std::runtime_error("poll");return std::move(state->next);}
  void cancel() noexcept override {++state->cancels;}
};
struct FakeProvider final:DiscoveryTaskProvider {
  FakeClock &clock; unsigned starts=0; bool throws=false, unavailable=false; std::uint64_t cost=0;
  DiscoveryTaskRequest last; std::shared_ptr<FakeJobState> job;
  explicit FakeProvider(FakeClock &c):clock(c){}
  std::unique_ptr<DiscoveryTask> start(const DiscoveryTaskRequest &r) override {
    ++starts;last=r;clock.value+=cost;if(throws)throw std::runtime_error("start");if(unavailable)return nullptr;
    job=std::make_shared<FakeJobState>();return std::make_unique<FakeJob>(job,clock);
  }
};
struct FakeSink final:DiscoveryCommitSink {
  FakeClock &clock; unsigned commits=0,retires=0; bool throws=false,accept=true, mutate=false; std::uint64_t cost=0;
  DiscoverySnapshot accepted; std::string reason;
  explicit FakeSink(FakeClock &c):clock(c){}
  bool commit(const DiscoveryTaskRequest &,DiscoveryTaskResult &r) override {
    ++commits;clock.value+=cost;if(throws)throw std::runtime_error("commit");accepted=r.snapshot;
    if(mutate)r.snapshot={};return accept;
  }
  void retire(const char *r) noexcept override {++retires;accepted={};reason=r;}
};
struct Rig {
  FakeClock clock; SessionProfile p=profile(); FakeProvider provider{clock}; FakeSink sink{clock};
  DiscoveryScheduleConfig config=enabled(); DiscoveryScheduler scheduler;
  explicit Rig(DiscoveryScheduleConfig c=enabled(),SessionProfile pr=profile()):p(std::move(pr)),config(c),scheduler(p,clock,provider,sink,c){}
  void start(){auto t=scheduler.tick();CHECK(t.started);CHECK(provider.job!=nullptr);CHECK(provider.job->polls==0);}
  DiscoveryTaskResult result(std::uint64_t revision=1,std::uint64_t handles=1) {
    DiscoveryTaskResult r;r.generation=provider.last.generation;r.kind=provider.last.kind;
    r.snapshot.revision=revision;r.snapshot.observed_us=clock.value;r.slot_observed_us={clock.value,clock.value};
    for(unsigned s=0;s<2;++s)r.snapshot.items.push_back({p.slots[s].expected,"handle-"+std::to_string(handles)+"-"+std::to_string(s),s==0,s==1?100U:0U});
    return r;
  }
  void deliver(DiscoveryTaskResult r){provider.job->next.state=DiscoveryPollState::Complete;provider.job->next.result=std::move(r);}
  void ready(std::uint64_t revision=1,std::uint64_t handles=1){start();deliver(result(revision,handles));CHECK(scheduler.tick().committed);}
  void reprobe(){clock.value=scheduler.next_due_us();start();CHECK(provider.last.kind==DiscoveryTaskKind::Reprobe);}
  void retry(){clock.value=scheduler.next_due_us();start();CHECK(provider.last.kind==DiscoveryTaskKind::Discover);}
};
void rejected(Rig &r,DiscoveryTaskResult v,const char *why=nullptr){r.deliver(std::move(v));auto t=r.scheduler.tick();CHECK(!t.committed);CHECK(r.scheduler.snapshot().items.empty());CHECK(r.sink.commits==0);CHECK(t.state==DiscoveryScheduleState::Backoff || t.state==DiscoveryScheduleState::Failed);if(why)CHECK(t.reason==why);}
}
int main() {
  unsigned passed=0,failed=0;
  const auto test=[&](const char *id,const std::function<void()> &fn){try{fn();++passed;std::cout<<"PASS "<<id<<'\n';}catch(const std::exception &e){++failed;std::cerr<<id<<": "<<e.what()<<'\n';}};
  test("p10_scheduler_unbound_inert",[]{Rig r(enabled(),SessionProfile{});for(unsigned i=0;i<100;++i)CHECK(r.scheduler.tick().state==DiscoveryScheduleState::Denied);CHECK(r.provider.starts==0&&r.clock.reads==0&&r.clock.waits==0&&r.sink.retires==0);r.scheduler.invalidate();r.scheduler.stop();CHECK(r.sink.retires==0);});
  test("p10_scheduler_bound_default_denied",[]{Rig r(DiscoveryScheduleConfig{});CHECK(r.scheduler.tick().state==DiscoveryScheduleState::Denied);CHECK(r.provider.starts==0);});
  test("p10_scheduler_invalid_profile_denied",[]{auto p=profile();p.can_tx_enabled=true;Rig r(enabled(),p);CHECK(r.scheduler.reason()=="INVALID_PROFILE");CHECK(!r.scheduler.tick().started);CHECK(r.provider.starts==0);});
  test("p10_scheduler_native_candidate_distinct",[]{Rig r(enabled(),profile(true));r.ready();CHECK(r.scheduler.snapshot().items[0].identity.identity.rfind("uid:",0)==0);CHECK(!r.p.device_access_enabled&&!r.p.can_tx_enabled&&!r.p.firmware_install_enabled);});
  test("p10_scheduler_invalid_config_denied",[]{for(unsigned n=0;n<10;++n){auto c=enabled();if(n==0)c.reprobe_interval_us=0;if(n==1)c.task_timeout_us=0;if(n==2)c.max_observation_age_us=kLoopMaxGapUs+1;if(n==3)c.reprobe_interval_us=c.max_observation_age_us;if(n==4)c.task_timeout_us=c.max_observation_age_us-c.reprobe_interval_us;if(n==5)c.initial_backoff_us=0;if(n==6)c.maximum_backoff_us=c.initial_backoff_us-1;if(n==7)c.maximum_backoff_us=10000001;if(n==8)c.callback_budget_us=0;if(n==9)c.callback_budget_us=kLoopBudgetUs+1;Rig r(c);CHECK(r.scheduler.reason()=="INVALID_SCHEDULE_CONFIG");CHECK(!r.scheduler.tick().started);CHECK(r.provider.starts==0);}});
  test("p10_scheduler_one_action_per_tick",[]{Rig r;r.start();CHECK(r.provider.starts==1&&r.provider.job->polls==0);CHECK(r.provider.last.kind==DiscoveryTaskKind::Discover);for(unsigned i=0;i<5;++i){CHECK(r.scheduler.tick().polled);CHECK(r.provider.starts==1);CHECK(r.provider.job->polls==i+1);}CHECK(r.clock.waits==0);});
  test("p10_scheduler_pending_leaves_cycle_available",[]{Rig r;r.start();unsigned health=0,heartbeats=0;for(unsigned n=0;n<50;++n){r.clock.value+=1000;CHECK(r.scheduler.tick().polled);++health;++heartbeats;}CHECK(health==50&&heartbeats==50&&r.provider.starts==1&&r.sink.commits==0&&r.clock.waits==0);});
  test("p10_scheduler_complete_group_original_min_time",[]{Rig r;r.start();auto v=r.result();r.clock.value+=3;v.slot_observed_us[1]+=2;std::reverse(v.snapshot.items.begin(),v.snapshot.items.end());r.deliver(std::move(v));CHECK(r.scheduler.tick().committed);CHECK(r.scheduler.snapshot().observed_us==1000000);CHECK(r.scheduler.next_due_us()==1050000);CHECK(r.sink.commits==1);});
  test("p10_scheduler_commit_cannot_mutate_validated_snapshot",[]{Rig r;r.sink.mutate=true;r.ready();CHECK(r.scheduler.snapshot().items.size()==2);});
  test("p10_scheduler_periodic_same_handle_reprobe",[]{Rig r;r.ready();const auto old=r.scheduler.snapshot();for(unsigned n=0;n<5;++n){r.reprobe();CHECK(r.provider.last.previous.items[0].handle_nonce==old.items[0].handle_nonce);r.deliver(r.result(n+2));CHECK(r.scheduler.tick().committed);CHECK(r.scheduler.snapshot().items[0].handle_nonce==old.items[0].handle_nonce);}CHECK(r.sink.retires==0&&r.sink.commits==6);});
  test("p10_scheduler_snapshot_read_does_not_retimestamp",[]{Rig r;r.ready();auto t=r.scheduler.snapshot().observed_us;r.clock.value+=20000;for(unsigned n=0;n<50;++n){CHECK(!r.scheduler.tick().started);CHECK(r.scheduler.snapshot().observed_us==t);}CHECK(r.provider.starts==1);});
  test("p10_scheduler_drop_timeout_cancels_whole_group",[]{Rig r;r.ready();r.reprobe();auto job=r.provider.job;r.clock.value=r.provider.last.deadline_us;auto t=r.scheduler.tick();CHECK(t.reason=="DISCOVERY_TASK_TIMEOUT");CHECK(t.retired);CHECK(job->cancels==1&&job->destroys==1);CHECK(r.scheduler.snapshot().items.empty());CHECK(r.sink.retires==1);r.retry();CHECK(r.provider.last.previous.items.empty());});
  test("p10_scheduler_expired_group_before_late_completion",[]{Rig r;r.ready();r.reprobe();r.deliver(r.result(2));auto job=r.provider.job;r.clock.value=1000000+kLoopMaxGapUs;auto t=r.scheduler.tick();CHECK(t.reason=="GROUP_OBSERVATION_EXPIRED");CHECK(!t.polled&&job->polls==0&&job->cancels==1);CHECK(r.sink.commits==1);});
  test("p10_scheduler_backoff_bounded_and_resets",[]{Rig r;std::uint64_t expected=10000;for(unsigned n=0;n<9;++n){r.start();r.provider.job->next.state=DiscoveryPollState::Failed;const auto now=r.clock.value;CHECK(r.scheduler.tick().state==DiscoveryScheduleState::Backoff);CHECK(r.scheduler.next_due_us()-now==expected);r.clock.value=r.scheduler.next_due_us()-1;CHECK(!r.scheduler.tick().started);++r.clock.value;expected=std::min<std::uint64_t>(500000,expected*2);}r.ready();r.scheduler.invalidate();CHECK(r.scheduler.tick().reason=="EXTERNAL_GROUP_INVALIDATED");CHECK(r.scheduler.next_due_us()-r.clock.value==10000);});
  test("p10_scheduler_stop_pending_is_permanent",[]{Rig r;r.start();auto job=r.provider.job;r.scheduler.stop();r.scheduler.stop();r.scheduler.invalidate();CHECK(job->cancels==1&&job->destroys==1);for(unsigned n=0;n<10;++n)CHECK(r.scheduler.tick().state==DiscoveryScheduleState::Stopped);CHECK(r.provider.starts==1&&r.sink.retires==1);});
  test("p10_scheduler_stop_ready_retires_once",[]{Rig r;r.ready();r.scheduler.stop();r.scheduler.stop();CHECK(r.sink.retires==1);CHECK(r.scheduler.snapshot().items.empty());});
  test("p10_scheduler_clock_rollback_fails_closed",[]{Rig r;r.ready();--r.clock.value;auto t=r.scheduler.tick();CHECK(t.state==DiscoveryScheduleState::Failed&&t.reason=="CLOCK_INVALID");CHECK(r.scheduler.snapshot().items.empty());r.clock.value+=1000000;CHECK(!r.scheduler.tick().started);});
  test("p10_scheduler_clock_zero_or_exception_denied",[]{for(bool exception:{false,true}){Rig r;r.start();r.clock.throws=exception;if(!exception)r.clock.value=0;CHECK(r.scheduler.tick().state==DiscoveryScheduleState::Failed);CHECK(r.provider.job->cancels==1);}});
  test("p10_scheduler_stale_generation_or_kind_rejected",[]{for(unsigned n=0;n<2;++n){Rig r;r.start();auto v=r.result();if(n==0)++v.generation;else v.kind=DiscoveryTaskKind::Reprobe;rejected(r,std::move(v),"STALE_TASK_COMPLETION");}});
  test("p10_scheduler_revision_zero_and_reuse_rejected",[]{Rig r;r.start();auto v=r.result(0);rejected(r,std::move(v),"DISCOVERY_REVISION_NOT_NEW");Rig a;a.ready();a.reprobe();a.deliver(a.result(1));CHECK(a.scheduler.tick().reason=="DISCOVERY_REVISION_NOT_NEW");CHECK(a.scheduler.snapshot().items.empty());});
  test("p10_scheduler_partial_extra_duplicate_group_rejected",[]{for(unsigned n=0;n<4;++n){Rig r;r.start();auto v=r.result();if(n==0)v.snapshot.items.pop_back();if(n==1)v.snapshot.items.push_back(v.snapshot.items.front());if(n==2)v.snapshot.items[1]=v.snapshot.items[0];if(n==3)v.snapshot.items[1].handle_nonce=v.snapshot.items[0].handle_nonce;rejected(r,std::move(v));}});
  test("p10_scheduler_every_identity_field_verified",[]{for(unsigned n=0;n<8;++n){Rig r;r.start();auto v=r.result();auto &id=v.snapshot.items[0].identity;if(n==0)id.identity="fixture:replacement";if(n==1)id.board_type="RED_PANDA";if(n==2)id.mcu_family="H7";if(n==3)id.exact_mcu="fixture:wrong";if(n==4)id.transport="SPI";if(n==5)id.protocol_profile="old";if(n==6)id.application_signature="fixture:other";if(n==7)id.capabilities.fan=false;rejected(r,std::move(v),"IDENTITY_OR_HANDLE_MISMATCH");}});
  test("p10_scheduler_handle_format_bounded",[]{for(unsigned n=0;n<3;++n){Rig r;r.start();auto v=r.result();v.snapshot.items[0].handle_nonce=n==0?"":n==1?std::string(129,'x'):"bad handle";rejected(r,std::move(v),"IDENTITY_OR_HANDLE_MISMATCH");}});
  test("p10_scheduler_activation_evidence_required",[]{for(unsigned n=0;n<4;++n){Rig r;r.start();auto v=r.result();if(n==0)v.snapshot.items[0].usb_alt0_verified=false;if(n==1)v.snapshot.items[0].spi_first_request_id=1;if(n==2)v.snapshot.items[1].usb_alt0_verified=true;if(n==3)v.snapshot.items[1].spi_first_request_id=0;rejected(r,std::move(v),"ACTIVATION_EVIDENCE_MISSING");}});
  test("p10_scheduler_no_fabricated_observation_time",[]{for(unsigned n=0;n<4;++n){Rig r;r.start();auto v=r.result();if(n==0)v.slot_observed_us[0]=0;if(n==1)v.slot_observed_us[0]=r.clock.value+1;if(n==2)v.slot_observed_us[0]=r.clock.value-1;if(n==3)++v.snapshot.observed_us;if(n!=3)v.snapshot.observed_us=std::min(v.slot_observed_us[0],v.slot_observed_us[1]);rejected(r,std::move(v));}});
  test("p10_scheduler_reprobe_rejects_old_completion_time",[]{Rig r;r.ready();auto old=r.scheduler.snapshot();r.reprobe();auto v=r.result(2);v.slot_observed_us={old.observed_us,old.observed_us};v.snapshot.observed_us=old.observed_us;r.deliver(std::move(v));CHECK(r.scheduler.tick().reason=="PROBE_TIME_INVALID_OR_STALE");CHECK(r.scheduler.snapshot().items.empty());});
  test("p10_scheduler_reprobe_never_changes_handle_or_spi_seed",[]{for(unsigned n=0;n<3;++n){Rig r;r.ready();r.reprobe();auto v=r.result(2);if(n==0)v.snapshot.items[0].handle_nonce="replacement";if(n==1)++v.snapshot.items[1].spi_first_request_id;if(n==2)v.snapshot.items[1].handle_nonce="replacement";r.deliver(std::move(v));CHECK(r.scheduler.tick().reason=="REPROBE_CHANGED_RETAINED_HANDLE");CHECK(r.scheduler.snapshot().items.empty());CHECK(r.sink.retires==1);}});
  test("p10_scheduler_reconnect_requires_both_fresh_leases",[]{for(unsigned n=0;n<2;++n){Rig r;r.ready();r.scheduler.invalidate();r.scheduler.tick();r.retry();auto v=r.result(2,2);v.snapshot.items[n].handle_nonce="handle-1-"+std::to_string(n);r.deliver(std::move(v));CHECK(r.scheduler.tick().reason=="RETIRED_HANDLE_REUSED");CHECK(r.scheduler.snapshot().items.empty());r.retry();r.deliver(r.result(3,3));CHECK(r.scheduler.tick().committed);CHECK(r.sink.commits==2);}});
  test("p10_scheduler_external_fault_cancels_reprobe",[]{Rig r;r.ready();r.reprobe();auto job=r.provider.job;r.scheduler.invalidate();CHECK(job->cancels==1);CHECK(r.scheduler.snapshot().items.empty());CHECK(r.sink.retires==1);CHECK(r.scheduler.tick().reason=="EXTERNAL_GROUP_INVALIDATED");});
  test("p10_scheduler_start_exception_and_busy_retry",[]{for(bool throws:{false,true}){Rig r;r.provider.throws=throws;r.provider.unavailable=!throws;auto t=r.scheduler.tick();CHECK(t.state==DiscoveryScheduleState::Backoff);CHECK(t.reason==(throws?"DISCOVERY_START_EXCEPTION":"DISCOVERY_WORKER_UNAVAILABLE"));CHECK(r.scheduler.snapshot().items.empty());CHECK(r.provider.starts==1);}});
  test("p10_scheduler_poll_failure_and_exception_retire",[]{for(bool throws:{false,true}){Rig r;r.ready();r.reprobe();r.provider.job->throws=throws;r.provider.job->next.state=DiscoveryPollState::Failed;auto t=r.scheduler.tick();CHECK(t.state==DiscoveryScheduleState::Backoff);CHECK(r.sink.retires==1);CHECK(r.scheduler.snapshot().items.empty());}});
  test("p10_scheduler_failed_handoff_consumes_pair",[]{for(bool throws:{false,true}){Rig r;r.start();r.sink.throws=throws;r.sink.accept=false;r.deliver(r.result());CHECK(r.scheduler.tick().state==DiscoveryScheduleState::Backoff);CHECK(r.sink.retires==1);r.sink.throws=false;r.sink.accept=true;r.retry();r.deliver(r.result(2));CHECK(r.scheduler.tick().reason=="RETIRED_HANDLE_REUSED");CHECK(r.sink.commits==1);}});
  test("p10_scheduler_slow_callback_terminal_gate",[]{for(unsigned n=0;n<3;++n){Rig r;if(n==0)r.provider.cost=1001;else {r.start();if(n==1)r.provider.job->poll_cost=1001;else {r.deliver(r.result());r.sink.cost=1001;}}auto t=r.scheduler.tick();CHECK(t.state==DiscoveryScheduleState::Failed);CHECK(t.reason=="NONBLOCKING_CALLBACK_BUDGET_EXCEEDED");CHECK(r.scheduler.snapshot().items.empty());CHECK(r.provider.job->cancels==1);CHECK(!r.scheduler.tick().started);}});
  test("p10_scheduler_deadline_crossed_during_poll",[]{Rig r;r.start();r.clock.value=r.provider.last.deadline_us-10;r.provider.job->poll_cost=10;r.deliver(r.result());auto t=r.scheduler.tick();CHECK(t.reason=="DISCOVERY_TASK_TIMEOUT");CHECK(r.sink.commits==0);});
  test("p10_scheduler_deadline_crossed_during_handoff",[]{Rig r;r.start();r.clock.value=r.provider.last.deadline_us-10;r.deliver(r.result());r.sink.cost=10;auto t=r.scheduler.tick();CHECK(t.reason=="DISCOVERY_HANDOFF_EXPIRED");CHECK(r.sink.commits==1&&r.sink.retires==1);CHECK(r.scheduler.snapshot().items.empty());});
  test("p10_scheduler_late_cancelled_job_never_published",[]{Rig r;r.start();auto old=r.provider.job;auto late=r.result();r.clock.value=r.provider.last.deadline_us;r.scheduler.tick();old->next.state=DiscoveryPollState::Complete;old->next.result=std::move(late);r.retry();r.deliver(r.result(2,2));CHECK(r.scheduler.tick().committed);CHECK(old->polls==0&&old->cancels==1);CHECK(r.scheduler.snapshot().items[0].handle_nonce=="handle-2-0");});
  test("p10_scheduler_history_budget_permanently_closes",[]{Rig r;for(unsigned n=1;n<=64;++n){r.ready(n,n);r.scheduler.invalidate();r.scheduler.tick();r.clock.value=r.scheduler.next_due_us();}r.start();r.deliver(r.result(65,65));auto t=r.scheduler.tick();CHECK(t.state==DiscoveryScheduleState::Failed);CHECK(t.reason=="HANDLE_HISTORY_EXHAUSTED");CHECK(r.sink.commits==64);});
  test("p10_scheduler_counter_overflow_never_wraps",[]{Rig r;r.clock.value=UINT64_MAX-5;auto t=r.scheduler.tick();CHECK(t.state==DiscoveryScheduleState::Failed);CHECK(t.reason=="SCHEDULE_COUNTER_EXHAUSTED");CHECK(r.provider.starts==0);});
  std::cout<<"{\"suite\":\"p10_discovery_scheduler\",\"passed\":"<<passed<<",\"failed\":"<<failed<<",\"skipped\":0,\"assertions\":"<<assertions<<",\"real_device_io\":false}\n";
  return failed?1:0;
}
