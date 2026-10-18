#include "discovery_scheduler.h"

#include <algorithm>
#include <limits>

namespace openpilot::pandad::compatibility {
namespace {
bool add(std::uint64_t a, std::uint64_t b, std::uint64_t &out) {
  if (b > UINT64_MAX-a) return false;
  out=a+b; return true;
}
bool same_item(const DiscoveryItem &a, const DiscoveryItem &b) {
  return a.identity==b.identity && a.handle_nonce==b.handle_nonce &&
    a.usb_alt0_verified==b.usb_alt0_verified && a.spi_first_request_id==b.spi_first_request_id;
}
bool valid_nonce(const std::string &s) {
  return !s.empty() && s.size()<=128 && std::all_of(s.begin(),s.end(),[](unsigned char c){return c>32 && c<127;});
}
}

DiscoveryScheduler::DiscoveryScheduler(const SessionProfile &profile, Clock &clock, DiscoveryTaskProvider &provider,
                                       DiscoveryCommitSink &sink, DiscoveryScheduleConfig config)
  : profile_(profile), config_(config), clock_(clock), provider_(provider), sink_(sink) {
  try { const SessionRegistry validated(profile_); (void)validated; }
  catch (...) { reason_="INVALID_PROFILE"; return; }
  if (!config_.allow_scheduling || (profile_.binding_status!="SYNTHETIC_BOUND" && profile_.binding_status!="NATIVE_READ_ONLY_BOUND")) return;
  if (!config_.reprobe_interval_us || !config_.task_timeout_us || !config_.max_observation_age_us ||
      config_.max_observation_age_us>kLoopMaxGapUs ||
      config_.reprobe_interval_us>=config_.max_observation_age_us ||
      config_.task_timeout_us>=config_.max_observation_age_us-config_.reprobe_interval_us ||
      !config_.initial_backoff_us || config_.maximum_backoff_us<config_.initial_backoff_us ||
      config_.maximum_backoff_us>10000000 || !config_.callback_budget_us || config_.callback_budget_us>kLoopBudgetUs) {
    reason_="INVALID_SCHEDULE_CONFIG"; return;
  }
  state_=DiscoveryScheduleState::Waiting; reason_="AWAITING_COMPLETE_DISCOVERY"; backoff_us_=config_.initial_backoff_us;
}
DiscoveryScheduler::~DiscoveryScheduler() { stop(); }
bool DiscoveryScheduler::read_clock(std::uint64_t &now) noexcept {
  try { now=clock_.now_us(); }
  catch (...) { return false; }
  if (!now || (clock_seen_ && now<last_clock_)) return false;
  last_clock_=now; clock_seen_=true; return true;
}
void DiscoveryScheduler::retire(const char *reason, DiscoveryTickResult *tick) noexcept {
  const bool affected=task_ || !snapshot_.items.empty();
  snapshot_={}; slot_observed_us_={};
  // The sink revokes runtime epochs before any cancellation can wake a worker
  // that owns native leases. Otherwise cleanup could race a still-live epoch.
  if (affected) { sink_.retire(reason); if (tick) tick->retired=true; }
  if (task_) { task_->cancel(); task_.reset(); }
}
void DiscoveryScheduler::fail(const char *reason, std::uint64_t now, bool terminal, DiscoveryTickResult *tick) noexcept {
  retire(reason,tick); reason_=reason;
  if (terminal || !add(now,backoff_us_,due_us_)) { state_=DiscoveryScheduleState::Failed; return; }
  state_=DiscoveryScheduleState::Backoff;
  // Saturating exponential retry, never an overflow or an immediate busy loop.
  backoff_us_=backoff_us_>config_.maximum_backoff_us/2 ? config_.maximum_backoff_us :
    std::min(config_.maximum_backoff_us,backoff_us_*2);
}
bool DiscoveryScheduler::callback_time(std::uint64_t before, std::uint64_t &after, DiscoveryTickResult &tick) {
  if (!read_clock(after)) { fail("CLOCK_INVALID",before,true,&tick); return false; }
  if (after-before>config_.callback_budget_us) { fail("NONBLOCKING_CALLBACK_BUDGET_EXCEEDED",after,true,&tick); return false; }
  return true;
}
bool DiscoveryScheduler::validate(const DiscoveryTaskResult &result, std::uint64_t now, const char *&reason) const {
  const auto reject=[&](const char *why) { reason=why; return false; };
  const auto &s=result.snapshot;
  if (result.generation!=request_.generation || result.kind!=request_.kind) return reject("STALE_TASK_COMPLETION");
  if (!s.revision || s.revision<=last_revision_) return reject("DISCOVERY_REVISION_NOT_NEW");
  if (s.items.size()!=2) return reject("COMPLETE_TWO_BOARD_GROUP_REQUIRED");
  std::array<const DiscoveryItem *,2> items{};
  for (const auto &item:s.items) {
    const auto slot=item.identity.identity==profile_.slots[0].expected.identity ? 0U:1U;
    if (!(item.identity==profile_.slots[slot].expected) || items[slot] || !valid_nonce(item.handle_nonce)) return reject("IDENTITY_OR_HANDLE_MISMATCH");
    if (item.identity.transport=="USB" ? (!item.usb_alt0_verified || item.spi_first_request_id) :
        item.identity.transport=="SPI" ? (item.usb_alt0_verified || !item.spi_first_request_id) : true)
      return reject("ACTIVATION_EVIDENCE_MISSING");
    items[slot]=&item;
  }
  if (!items[0] || !items[1] || items[0]->handle_nonce==items[1]->handle_nonce) return reject("DUPLICATE_OR_MISSING_HANDLE");
  if (s.observed_us!=std::min(result.slot_observed_us[0],result.slot_observed_us[1])) return reject("ORIGINAL_PROBE_TIME_REQUIRED");
  for (std::size_t slot=0;slot<2;++slot) {
    const auto stamp=result.slot_observed_us[slot];
    if (!stamp || stamp<request_.started_us || stamp>now || now-stamp>=config_.max_observation_age_us)
      return reject("PROBE_TIME_INVALID_OR_STALE");
    if (request_.kind==DiscoveryTaskKind::Reprobe) {
      const auto previous=std::find_if(snapshot_.items.begin(),snapshot_.items.end(),[&](const auto &item){return item.identity.identity==items[slot]->identity.identity;});
      if (previous==snapshot_.items.end() || !same_item(*previous,*items[slot])) return reject("REPROBE_CHANGED_RETAINED_HANDLE");
      if (stamp<=slot_observed_us_[slot]) return reject("REPROBE_DID_NOT_ADVANCE_EVIDENCE");
    } else if (consumed_.count(items[slot]->handle_nonce)) return reject("RETIRED_HANDLE_REUSED");
  }
  if (request_.kind==DiscoveryTaskKind::Discover && consumed_.size()>126) return reject("HANDLE_HISTORY_EXHAUSTED");
  return true;
}
DiscoveryTickResult DiscoveryScheduler::tick() {
  DiscoveryTickResult out;
  const auto finish=[&] { out.state=state_; out.reason=reason_; return out; };
  if (state_==DiscoveryScheduleState::Denied || state_==DiscoveryScheduleState::Stopped || state_==DiscoveryScheduleState::Failed) return finish();
  std::uint64_t now=0;
  if (!read_clock(now)) { fail("CLOCK_INVALID",last_clock_,true,&out); return finish(); }
  if (invalidated_) { invalidated_=false; fail("EXTERNAL_GROUP_INVALIDATED",now,false,&out); return finish(); }
  if (!snapshot_.items.empty() && (snapshot_.observed_us>now || now-snapshot_.observed_us>=config_.max_observation_age_us)) {
    fail("GROUP_OBSERVATION_EXPIRED",now,false,&out); return finish();
  }
  if (task_) {
    if (now>=request_.deadline_us) { fail("DISCOVERY_TASK_TIMEOUT",now,false,&out); return finish(); }
    DiscoveryPollResult poll;
    try { out.polled=true; poll=task_->poll(); }
    catch (...) { fail("DISCOVERY_POLL_EXCEPTION",now,false,&out); return finish(); }
    if (!callback_time(now,now,out)) return finish();
    if (now>=request_.deadline_us) { fail("DISCOVERY_TASK_TIMEOUT",now,false,&out); return finish(); }
    if (poll.state==DiscoveryPollState::Pending) return finish();
    if (poll.state!=DiscoveryPollState::Complete) { fail("DISCOVERY_TASK_FAILED",now,false,&out); return finish(); }
    const char *invalid="INVALID_DISCOVERY_RESULT";
    if (!validate(poll.result,now,invalid)) {
      fail(invalid,now,std::string(invalid)=="HANDLE_HISTORY_EXHAUSTED",&out); return finish();
    }
    // Consume leases before handoff, including a failed/throwing handoff.
    if (request_.kind==DiscoveryTaskKind::Discover) for (const auto &item:poll.result.snapshot.items) consumed_.insert(item.handle_nonce);
    last_revision_=poll.result.snapshot.revision;
    auto accepted_snapshot=poll.result.snapshot;
    const auto accepted_stamps=poll.result.slot_observed_us;
    bool committed=false;
    try { committed=sink_.commit(request_,poll.result); }
    catch (...) { fail("DISCOVERY_COMMIT_EXCEPTION",now,false,&out); return finish(); }
    if (!callback_time(now,now,out)) return finish();
    if (!committed) { fail("DISCOVERY_COMMIT_REJECTED",now,false,&out); return finish(); }
    if (now>=request_.deadline_us || now-accepted_snapshot.observed_us>=config_.max_observation_age_us) {
      fail("DISCOVERY_HANDOFF_EXPIRED",now,false,&out); return finish();
    }
    task_.reset();
    snapshot_=std::move(accepted_snapshot); slot_observed_us_=accepted_stamps;
    // Schedule from actual oldest evidence, not the time a mailbox was polled.
    if (!add(snapshot_.observed_us,config_.reprobe_interval_us,due_us_)) {
      fail("SCHEDULE_COUNTER_EXHAUSTED",now,true,&out); return finish();
    }
    backoff_us_=config_.initial_backoff_us;
    state_=DiscoveryScheduleState::Ready; reason_="COMPLETE_GROUP_EVIDENCE_CURRENT"; out.committed=true;
    return finish();
  }
  if (now<due_us_) return finish();
  if (next_generation_==UINT64_MAX || !add(now,config_.task_timeout_us,request_.deadline_us)) {
    fail("SCHEDULE_COUNTER_EXHAUSTED",now,true,&out); return finish();
  }
  request_.generation=++next_generation_; request_.started_us=now; request_.previous=snapshot_;
  request_.kind=snapshot_.items.empty() ? DiscoveryTaskKind::Discover:DiscoveryTaskKind::Reprobe;
  try { out.started=true; task_=provider_.start(request_); }
  catch (...) { fail("DISCOVERY_START_EXCEPTION",now,false,&out); return finish(); }
  if (!callback_time(now,now,out)) return finish();
  if (!task_) { fail("DISCOVERY_WORKER_UNAVAILABLE",now,false,&out); return finish(); }
  if (now>=request_.deadline_us) { fail("DISCOVERY_TASK_TIMEOUT",now,false,&out); return finish(); }
  state_=request_.kind==DiscoveryTaskKind::Discover ? DiscoveryScheduleState::Discovering:DiscoveryScheduleState::Reprobing;
  reason_=request_.kind==DiscoveryTaskKind::Discover ? "COMPLETE_DISCOVERY_PENDING":"RETAINED_HANDLE_REPROBE_PENDING";
  return finish();
}
void DiscoveryScheduler::stop() noexcept {
  if (state_==DiscoveryScheduleState::Stopped) return;
  retire("SCHEDULER_STOPPED"); state_=DiscoveryScheduleState::Stopped; reason_="SCHEDULER_STOPPED";
}
void DiscoveryScheduler::invalidate() noexcept {
  if (state_==DiscoveryScheduleState::Denied || state_==DiscoveryScheduleState::Stopped || state_==DiscoveryScheduleState::Failed) return;
  retire("EXTERNAL_GROUP_INVALIDATED"); invalidated_=true;
}
}  // namespace openpilot::pandad::compatibility
