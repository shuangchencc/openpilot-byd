#include "native_discovery_executor.h"

#include <algorithm>
#include <condition_variable>
#include <mutex>
#include <thread>
#include <unistd.h>

namespace openpilot::pandad::compatibility {
namespace {
std::atomic<std::size_t> worker_lanes{0};
bool read_only(const Request &r,std::uint32_t timeout) {
  if(!valid_request(r) || !timeout || timeout>kBackendSliceMs) return false;
  if(r.kind==Kind::BulkRead) return r.endpoint==0x81;
  if(r.kind!=Kind::ControlRead || !r.payload.empty() || r.index) return false;
  const unsigned n=r.request==0xc1?1:r.request==0xc3?12:r.request==0xdd?8:
    (r.request==0xd3 || r.request==0xd4 || r.request==0xc2)?64:r.request==0xd5?16:r.request==0xd2?48:r.request==0xb2?2:0;
  return n && r.read_length==n && (r.request==0xc2?r.value<3:r.value==0);
}
bool same_item(const DiscoveryItem &a,const DiscoveryItem &b) {
  return a.identity==b.identity && a.handle_nonce==b.handle_nonce && a.usb_alt0_verified==b.usb_alt0_verified && a.spi_first_request_id==b.spi_first_request_id;
}
class PortProxy final:public Backend {
  std::shared_ptr<NativeDiscoveryPort> port_;
  std::shared_ptr<Clock> clock_lifetime_;
  std::atomic<bool> cancelled_{false};
  const pid_t process_=getpid();
public:
  PortProxy(std::shared_ptr<NativeDiscoveryPort> p,std::shared_ptr<Clock> clock):port_(std::move(p)),clock_lifetime_(std::move(clock)){}
  Result transact(const Request &r,std::uint32_t timeout) override {
    if(getpid()!=process_ || cancelled_ || !port_->active()) return {Status::Cancelled,0,{},true,false};
    if(!read_only(r,timeout)) return {Status::Invalid,0,{},true,false};
    return port_->transact(r,timeout);
  }
  void cancel() noexcept override {if(getpid()!=process_)return;cancelled_=true;port_->cancel();}
};
class RetainedPort final:public NativeDiscoveryPort {
public:
  const pid_t process=getpid();
  std::mutex mutex;
  OwnedTransport *owned=nullptr;  // lifetime and all destruction belong to engine
  std::atomic<bool> enabled{false};
  Result transact(const Request &r,std::uint32_t timeout) override {
    if(getpid()!=process)return {Status::Cancelled,0,{},true,false};
    if(!read_only(r,timeout)) return {Status::Invalid,0,{},true,false};
    std::unique_lock<std::mutex> guard(mutex,std::try_to_lock);
    if(!guard.owns_lock()) return {Status::Busy,0,{},true,false};
    if(!enabled || !owned) return {Status::Cancelled,0,{},true,false};
    return owned->backend->transact(r,timeout);
  }
  void cancel() noexcept override {if(getpid()==process)enabled=false;}
  bool active() const override {return getpid()==process && enabled.load();}
  std::optional<SpiFreshnessProof> live_spi_proof() override {
    if(getpid()!=process)return std::nullopt;
    std::unique_lock<std::mutex> guard(mutex,std::try_to_lock);
    if(!guard.owns_lock() || !enabled || !owned) return std::nullopt;
    const auto *spi=dynamic_cast<SpiFreshnessBackend *>(owned->backend.get());
    if(!spi) return std::nullopt;
    const auto p=spi->proof();
    if(!p.synchronized || !p.same_fd_lifetime_lock) return std::nullopt;
    return p;
  }
};
bool api_shape(const UsbDeviceInfo &d) {
  if(!d.id || (d.vendor!=0xbbaa && d.vendor!=0x3801) || d.product!=0xddcc || (d.bcd_device&255) ||
      !d.serial_index || !d.active_configuration_known || d.alternates.size()>kUsbMaxAlternates) return false;
  unsigned alts=0;bool rx=false,tx=false;
  for(const auto &alt:d.alternates) if(alt.interface_number==0 && alt.alternate_setting==0) {
    ++alts;
    if(alt.endpoints.size()>kUsbMaxEndpoints)return false;
    std::set<unsigned> endpoints;
    for(const auto &ep:alt.endpoints) {
      if(!endpoints.insert(ep.address).second)return false;
      if(ep.address==0x81 && (ep.attributes&3)==2 && ep.max_packet_size==64) rx=true;
      if(ep.address==0x03 && (ep.attributes&3)==2 && ep.max_packet_size==64) tx=true;
    }
  }
  return alts==1 && rx && tx;
}
class ConcreteEngine final:public NativeDiscoveryEngine {
  const pid_t process_=getpid();
  NativeDiscoveryEngineConfig config_;
  std::array<std::unique_ptr<OwnedTransport>,2> owned_;
  std::array<std::shared_ptr<RetainedPort>,2> ports_;
  std::array<NativeObservedBoard,2> observed_;
  NativeBindingResult binding_;
  std::uint64_t revision_=0;
  void disable_ports() noexcept {for(auto &port:ports_)if(port)port->enabled=false;}
  bool current(const DiscoveryTaskRequest &r,const std::atomic<bool> &cancelled) const {
    if(cancelled || !config_.clock) return false;
    const auto now=config_.clock->now_us();
    return now>=r.started_us && now<r.deadline_us;
  }
  std::uint32_t remaining(const DiscoveryTaskRequest &r) const {
    const auto now=config_.clock->now_us();
    if(now>=r.deadline_us) return 0;
    return static_cast<std::uint32_t>(std::min<std::uint64_t>(100,(r.deadline_us-now)/1000));
  }
  bool configured() const {
    if(!config_.allow_read_only_device_io || !config_.clock || !config_.physical_evidence ||
        config_.profile.schema!="songplus-native-binding/v1" || config_.profile.binding_status!="CANDIDATE" ||
        config_.profile.device_access_enabled || config_.profile.can_tx_enabled || config_.profile.firmware_install_enabled) return false;
    // This checks only persisted shape and referenced physical evidence. It is
    // never handed off as a discovered binding or used as a lease identity.
    std::vector<NativeObservedBoard> shape;
    for(unsigned slot=0;slot<2;++slot) {
      const auto &b=config_.profile.slots[slot];
      if((b.transport=="USB" && !config_.usb_session_alt0) ||
          (b.transport=="SPI" && (!config_.spi_cooperative_ownership || !config_.spi_cooperative_ownership(b)))) return false;
      NativeObservedBoard o;o.raw=b.expected;o.raw.lease_nonce=slot+1;o.transport=b.transport;
      o.usb_alt0_verified=b.transport=="USB";o.spi_first_request_id=b.transport=="SPI"?config_.spi_first_request_id:0;
      o.spi_selector=b.spi_selector;shape.push_back(std::move(o));
    }
    if(shape[0].transport=="SPI" && shape[1].transport=="SPI" &&
       (shape[0].spi_selector.path==shape[1].spi_selector.path ||
        (shape[0].spi_selector.expected_major==shape[1].spi_selector.expected_major && shape[0].spi_selector.expected_minor==shape[1].spi_selector.expected_minor))) return false;
    return validate_native_binding(config_.profile,shape,config_.physical_evidence).bound;
  }
  std::unique_ptr<LibusbApi> usb_api() const {return config_.usb_api?config_.usb_api():make_native_libusb_api();}
  bool scan_usb(const DiscoveryTaskRequest &r,const std::atomic<bool> &cancelled) const {
    std::vector<unsigned> expected;
    for(const auto &b:config_.profile.slots)if(b.transport=="USB")expected.push_back(b.expected.board_type);
    if(expected.empty()) return true;
    if(!current(r,cancelled))return false;
    auto api=usb_api();if(!api)return false;
    struct Cleanup {LibusbApi *api;~Cleanup(){api->shutdown();}} cleanup{api.get()};
    if(api->initialize()!=0 || !current(r,cancelled))return false;
    std::vector<UsbDeviceInfo> devices;
    if(api->enumerate(devices)!=0 || !current(r,cancelled) || devices.size()!=expected.size())return false;
    std::vector<unsigned> actual;
    std::set<UsbDeviceId> ids;
    for(const auto &d:devices){if(!api_shape(d) || !ids.insert(d.id).second)return false;actual.push_back(d.bcd_device>>8);}
    std::sort(expected.begin(),expected.end());std::sort(actual.begin(),actual.end());
    return expected==actual;
  }
  bool read_identity(unsigned slot,const DiscoveryTaskRequest &task,const std::atomic<bool> &cancelled) {
    auto &port=*ports_[slot];
    std::unique_lock<std::mutex> guard(port.mutex,std::try_to_lock);
    if(!guard.owns_lock() || !port.enabled || !port.owned || !current(task,cancelled))return false;
    auto next=observed_[slot].raw;
    const auto read=[&](unsigned request,auto &bytes) {
      if(!current(task,cancelled))return false;
      Request r;r.request=request;r.read_length=bytes.size();
      const auto budget=remaining(task);if(!budget)return false;
      const auto result=port.owned->transport->perform(r,budget);
      if(result.status!=Status::Ok || !result.actual_known || result.delivery_uncertain || result.actual!=bytes.size() || result.data.size()!=bytes.size() || !current(task,cancelled))return false;
      std::copy(result.data.begin(),result.data.end(),bytes.begin());return true;
    };
    std::array<std::uint8_t,1> board{};
    std::array<std::uint8_t,64> first{},second{};
    if(!read(0xc3,next.uid) || !read(0xc1,board) || !read(0xdd,next.protocol) || !read(0xd3,first) || !read(0xd4,second))return false;
    next.board_type=board[0];std::copy(first.begin(),first.end(),next.application_signature.begin());std::copy(second.begin(),second.end(),next.application_signature.begin()+64);
    if(next.board_type==6 && (next.application_signature!=observed_[slot].raw.application_signature || !read(0xd5,next.f4_capabilities)))return false;
    if(!(next==observed_[slot].raw))return false;
    if(observed_[slot].transport=="USB" && !config_.usb_session_alt0(next))return false;
    if(observed_[slot].transport=="SPI") {
      const auto *spi=dynamic_cast<SpiFreshnessBackend *>(owned_[slot]->backend.get());
      if(!spi)return false;
      const auto proof=spi->proof();
      if(!proof.synchronized || !proof.same_fd_lifetime_lock || proof.lease_nonce!=next.lease_nonce || proof.expected_uid!=next.uid)return false;
    }
    next.observed_us=config_.clock->now_us();
    if(!current(task,cancelled))return false;
    observed_[slot].raw=next;return true;
  }
  bool discover(const DiscoveryTaskRequest &r,const std::atomic<bool> &cancelled) {
    if(owned_[0] || owned_[1])return false;
    if(!configured() || !current(r,cancelled))return false;
    NativeUsbProbePolicy usb_policy;
    for(const auto &b:config_.profile.slots){
      if(b.transport=="USB")++usb_policy.expected_count;
      if(b.expected.board_type==6 && std::find(usb_policy.f4_application_signatures.begin(),usb_policy.f4_application_signatures.end(),b.expected.application_signature)==usb_policy.f4_application_signatures.end())
        usb_policy.f4_application_signatures.push_back(b.expected.application_signature);
    }
    usb_policy.budget_ms=remaining(r);if(!usb_policy.budget_ms)return false;
    NativeUsbInventory usb;
    if(usb_policy.expected_count){usb=discover_native_usb(usb_api(),*config_.clock,usb_policy,true);if(!usb.complete() || !current(r,cancelled))return false;}
    std::vector<NativeObservedBoard> inventory;
    const auto usb_raw=usb.observations();
    for(const auto &raw:usb_raw){NativeObservedBoard o;o.raw=raw;o.transport="USB";o.usb_alt0_verified=config_.usb_session_alt0(raw);inventory.push_back(std::move(o));}
    std::array<NativeSpiProbeResult,2> spi;
    for(unsigned slot=0;slot<2;++slot) {
      const auto &b=config_.profile.slots[slot];if(b.transport!="SPI")continue;
      NativeSpiProbePolicy p;p.selector=b.spi_selector;p.first_request_id=config_.spi_first_request_id;p.budget_ms=remaining(r);p.f4_application_signatures=usb_policy.f4_application_signatures;
      if(!p.budget_ms || !current(r,cancelled))return false;
      auto fresh=discover_fresh_native_spi(config_.spi_syscalls?config_.spi_syscalls():make_linux_spi_syscalls(),*config_.clock,p,b.expected.uid,config_.spi_cooperative_ownership(b),true);
      if(!fresh.complete() || !current(r,cancelled))return false;
      NativeObservedBoard o;o.raw=fresh.probe.observation;o.transport="SPI";o.spi_first_request_id=fresh.probe.first_request_id;o.spi_selector=fresh.probe.validated_selector;
      inventory.push_back(o);spi[slot]=std::move(fresh.probe);
    }
    binding_=validate_native_binding(config_.profile,inventory,config_.physical_evidence);if(!binding_.bound)return false;
    for(unsigned slot=0;slot<2;++slot) {
      const auto &o=binding_.observations[slot];NativeLease lease;
      if(o.transport=="USB"){
        const auto it=std::find_if(usb_raw.begin(),usb_raw.end(),[&](const auto &raw){return raw.lease_nonce==o.raw.lease_nonce;});
        if(it==usb_raw.end())return false;
        lease=native_usb_read_only_lease(usb,static_cast<std::size_t>(it-usb_raw.begin()),binding_,slot,*config_.clock,true);
      } else lease=native_spi_read_only_lease(std::move(spi[slot]),o);
      if(!lease.io || !lease.observed_us || !current(r,cancelled))return false;
      observed_[slot]=lease.observed;observed_[slot].raw.observed_us=lease.observed_us;
      owned_[slot]=std::move(lease.io);ports_[slot]=std::make_shared<RetainedPort>();ports_[slot]->owned=owned_[slot].get();ports_[slot]->enabled=true;
    }
    return true;
  }
public:
  explicit ConcreteEngine(NativeDiscoveryEngineConfig c):config_(std::move(c)){}
  ~ConcreteEngine() override {
    if(getpid()==process_)retire();
    else for(auto &owned:owned_)(void)owned.release(); // inherited device owners remain quarantined in a fork child
  }
  void retire() noexcept override {
    if(getpid()!=process_)return;
    disable_ports();
    for(unsigned slot=0;slot<2;++slot){if(ports_[slot]){std::lock_guard<std::mutex> guard(ports_[slot]->mutex);ports_[slot]->owned=nullptr;owned_[slot].reset();}else owned_[slot].reset();ports_[slot].reset();}
    observed_={};binding_={};
  }
  DiscoveryTaskResult execute(const DiscoveryTaskRequest &r,const std::atomic<bool> &cancelled) override {
    DiscoveryTaskResult out;out.generation=r.generation;out.kind=r.kind;
    if(getpid()!=process_)return out;
    const bool had_group=owned_[0] || owned_[1];
    try {
      if(!r.generation || !r.started_us || r.deadline_us<=r.started_us || !current(r,cancelled)){disable_ports();return out;}
      if(r.kind==DiscoveryTaskKind::Discover){
        // A new discovery cannot replace a published group without owner
        // epoch revocation. A caller must first request retire on the worker.
        if(owned_[0] || owned_[1]){disable_ports();return out;}
        if(!discover(r,cancelled)){retire();return out;}
      }
      else {
        if(!binding_.bound || r.previous.items.size()!=2 || !scan_usb(r,cancelled)){disable_ports();return out;}
        for(unsigned slot=0;slot<2;++slot) {
          const auto &o=observed_[slot];const std::string nonce=o.transport+":"+std::to_string(o.raw.lease_nonce);
          const auto it=std::find_if(r.previous.items.begin(),r.previous.items.end(),[&](const auto &item){return item.identity==binding_.profile.slots[slot].expected;});
          if(it==r.previous.items.end() || it->handle_nonce!=nonce || it->usb_alt0_verified!=o.usb_alt0_verified ||
              it->spi_first_request_id!=o.spi_first_request_id || !read_identity(slot,r,cancelled)){disable_ports();return out;}
        }
      }
      if(!current(r,cancelled) || revision_==UINT64_MAX){disable_ports();return out;}
      out.snapshot.revision=++revision_;out.snapshot.observed_us=UINT64_MAX;
      auto handoff=std::make_unique<NativeDiscoveryHandoff>();handoff->profile=binding_.profile;
      for(unsigned slot=0;slot<2;++slot){const auto &o=observed_[slot];out.slot_observed_us[slot]=o.raw.observed_us;out.snapshot.observed_us=std::min(out.snapshot.observed_us,o.raw.observed_us);
        out.snapshot.items.push_back({binding_.profile.slots[slot].expected,o.transport+":"+std::to_string(o.raw.lease_nonce),o.usb_alt0_verified,o.spi_first_request_id});handoff->ports[slot]=ports_[slot];}
      out.payload=std::move(handoff);return out;
    } catch(...){if(r.kind==DiscoveryTaskKind::Discover && !had_group)retire();else disable_ports();return out;}
  }
};
struct Mailbox {
  std::atomic<bool> cancelled{false};
  std::atomic<bool> consumed{false};
  DiscoveryTaskKind kind=DiscoveryTaskKind::Discover;
  std::uint64_t generation=0;
  std::atomic<bool> committed{false};
  const DiscoveryTaskPayload *delivered_payload=nullptr;
  std::mutex mutex;
  DiscoveryPollResult result;
  bool ready=false;
};
}

std::unique_ptr<NativeDiscoveryEngine> make_native_discovery_engine(NativeDiscoveryEngineConfig c){return std::make_unique<ConcreteEngine>(std::move(c));}
struct NativeDiscoveryExecutor::State {
  const pid_t process=getpid();
  std::unique_ptr<NativeDiscoveryEngine> engine;
  std::mutex mutex;std::condition_variable wake;
  std::atomic<bool> stop{false},retire_requested{false},cancel_requested{false},busy{false};
  bool worker_started=false;
  std::optional<DiscoveryTaskRequest> pending;
  std::shared_ptr<Mailbox> pending_mailbox,owner_mailbox;
  static void work(const std::shared_ptr<State> &s) {
    for(;;) {
      DiscoveryTaskRequest request;std::shared_ptr<Mailbox> box;
      {std::unique_lock<std::mutex> guard(s->mutex);s->wake.wait(guard,[&]{return s->stop || s->retire_requested || s->cancel_requested || s->pending.has_value();});
        if(s->stop)break;
        if(s->retire_requested.exchange(false)){s->busy=true;s->cancel_requested=false;s->pending.reset();s->pending_mailbox.reset();guard.unlock();s->engine->retire();s->busy=false;continue;}
        if(s->cancel_requested.exchange(false)) {
          s->busy=true;
          const bool uncommitted=s->owner_mailbox && !s->owner_mailbox->consumed && s->owner_mailbox->kind==DiscoveryTaskKind::Discover;
          s->pending.reset();s->pending_mailbox.reset();guard.unlock();if(uncommitted)s->engine->retire();s->busy=false;continue;
        }
        request=std::move(*s->pending);s->pending.reset();box=std::move(s->pending_mailbox);}
      DiscoveryPollResult result;
      try{result.result=s->engine->execute(request,box->cancelled);result.state=DiscoveryPollState::Complete;}
      catch(...){result.state=DiscoveryPollState::Failed;}
      const bool owner_retired=s->retire_requested.exchange(false);
      (void)s->cancel_requested.exchange(false);
      if(box->cancelled || s->stop || owner_retired){
        if(s->stop || owner_retired || request.kind==DiscoveryTaskKind::Discover)s->engine->retire();
        // A revoke arriving while cleanup runs stays queued for the next
        // worker iteration; never erase it with a trailing plain store.
        s->busy=false;
      }
      else {std::lock_guard<std::mutex> guard(box->mutex);box->result=std::move(result);box->ready=true;}
    }
    s->engine->retire();s->engine.reset();s->busy=false;
    worker_lanes.fetch_sub(1);
  }
};
class NativeDiscoveryExecutor::Task final:public DiscoveryTask {
  std::shared_ptr<State> state_;std::shared_ptr<Mailbox> box_;
public:
  Task(std::shared_ptr<State> s,std::shared_ptr<Mailbox> b):state_(std::move(s)),box_(std::move(b)){}
  ~Task() override {cancel();}
  DiscoveryPollResult poll() override {
    if(getpid()!=state_->process){DiscoveryPollResult out;out.state=DiscoveryPollState::Failed;return out;}
    std::unique_lock<std::mutex> guard(box_->mutex,std::try_to_lock);
    if(!guard.owns_lock() || !box_->ready)return {};
    if(box_->consumed || box_->cancelled){DiscoveryPollResult out;out.state=DiscoveryPollState::Failed;return out;}
    box_->delivered_payload=box_->result.result.payload.get();box_->consumed=true;state_->busy=false;return std::move(box_->result);
  }
  void cancel() noexcept override {
    if(getpid()!=state_->process || box_->consumed || box_->cancelled.exchange(true))return;
    state_->busy=true;state_->cancel_requested=true;state_->wake.notify_one();
  }
};
NativeDiscoveryExecutor::NativeDiscoveryExecutor(std::unique_ptr<NativeDiscoveryEngine> engine,std::shared_ptr<Clock> clock,NativeEpochRetirer revoke,bool allow)
  :state_(std::make_shared<State>()),clock_(std::move(clock)),revoke_(std::move(revoke)),allowed_(allow){state_->engine=std::move(engine);}
NativeDiscoveryExecutor::~NativeDiscoveryExecutor(){
  // A fork child has no worker thread but inherits its shared State reference.
  // Keep that inherited, process-local quarantine; never reset the global lane
  // or touch copied mutexes/device owners. No new reference/allocation is made.
  // If a worker never started, its engine is inert and ordinary destruction is
  // safe (the concrete engine also rejects cross-process device cleanup).
  if(getpid()!=state_->process)return;
  retire("EXECUTOR_DESTROYED");state_->stop=true;state_->wake.notify_one();
}
std::size_t NativeDiscoveryExecutor::occupied_worker_lanes(){return worker_lanes.load();}
std::unique_ptr<DiscoveryTask> NativeDiscoveryExecutor::start(const DiscoveryTaskRequest &r) {
  if(getpid()!=state_->process || !allowed_ || !clock_ || !state_->engine || state_->stop || state_->busy || state_->retire_requested || state_->cancel_requested || !r.generation || !r.started_us || r.deadline_us<=r.started_us)return nullptr;
  std::unique_lock<std::mutex> guard(state_->mutex,std::try_to_lock);if(!guard.owns_lock() || state_->busy)return nullptr;
  if(!state_->worker_started) {
    std::size_t empty=0;if(!worker_lanes.compare_exchange_strong(empty,1))return nullptr;
    try{std::thread worker([state=state_]{State::work(state);});worker.detach();state_->worker_started=true;}
    catch(...){worker_lanes.fetch_sub(1);return nullptr;}
  }
  auto box=std::make_shared<Mailbox>();box->kind=r.kind;box->generation=r.generation;state_->pending=r;state_->pending_mailbox=box;state_->owner_mailbox=box;state_->busy=true;
  state_->wake.notify_one();return std::make_unique<Task>(state_,std::move(box));
}
bool NativeDiscoveryExecutor::commit(const DiscoveryTaskRequest &r,DiscoveryTaskResult &result) {
  if(getpid()!=state_->process || !allowed_ || state_->stop || state_->retire_requested || !state_->owner_mailbox)return false;
  const auto box=state_->owner_mailbox;
  if(box->cancelled || !box->consumed || box->committed || box->generation!=r.generation || box->kind!=r.kind || box->delivered_payload!=result.payload.get())return false;
  // Every attempted delivery is single-use, including a rejected handoff.
  box->committed=true;
  if(result.generation!=r.generation || result.kind!=r.kind || result.snapshot.items.size()!=2 ||
      result.snapshot.revision<=last_committed_revision_ || !result.snapshot.observed_us || !clock_)return false;
  std::uint64_t now=0;try{now=clock_->now_us();}catch(...){return false;}
  if(now>=r.deadline_us || result.snapshot.observed_us!=std::min(result.slot_observed_us[0],result.slot_observed_us[1]))return false;
  for(const auto stamp:result.slot_observed_us)if(!stamp || stamp<r.started_us || stamp>now || now-stamp>=kLoopMaxGapUs)return false;
  auto *handoff=dynamic_cast<NativeDiscoveryHandoff *>(result.payload.get());if(!handoff)return false;
  try{SessionRegistry checked(handoff->profile);(void)checked;}catch(...){return false;}
  if(handoff->profile.binding_status!="NATIVE_READ_ONLY_BOUND")return false;
  std::array<DiscoveryItem,2> items;
  for(unsigned slot=0;slot<2;++slot){const auto it=std::find_if(result.snapshot.items.begin(),result.snapshot.items.end(),[&](const auto &item){return item.identity==handoff->profile.slots[slot].expected;});
    if(it==result.snapshot.items.end() || !handoff->ports[slot] || !handoff->ports[slot]->active()) return false;
    items[slot]=*it;
  }
  if(r.kind==DiscoveryTaskKind::Reprobe){if(committed_.items.size()!=2)return false;for(unsigned slot=0;slot<2;++slot)if(ports_[slot]!=handoff->ports[slot] || !same_item(committed_.items[slot],items[slot]))return false;}
  else {if(!committed_.items.empty())return false;ports_=handoff->ports;opened_={};}
  committed_=result.snapshot;committed_.items={items[0],items[1]};committed_profile_=handoff->profile;last_committed_revision_=result.snapshot.revision;return true;
}
void NativeDiscoveryExecutor::retire(const char *reason) noexcept {
  if(getpid()!=state_->process)return;
  if(!committed_.items.empty() || state_->busy){try{if(revoke_)revoke_(reason);}catch(...){/* still revoke all ports */}}
  for(auto &p:ports_) { if(p) p->cancel(); }
  ports_={};opened_={};committed_={};committed_profile_={};
  if(state_->owner_mailbox)state_->owner_mailbox->cancelled=true;
  if(state_->worker_started){state_->busy=true;state_->retire_requested=true;state_->wake.notify_one();}
}
std::unique_ptr<OwnedTransport> NativeDiscoveryExecutor::open(const DiscoveryItem &item,const SessionBinding &binding) {
  if(getpid()!=state_->process || binding.slot>=2 || binding.safety_config_semantics!="noOutput" || committed_.items.size()!=2 || !clock_)return nullptr;
  const auto slot=binding.slot;
  if(opened_[slot] || !ports_[slot] || !ports_[slot]->active() || !(binding.expected==committed_profile_.slots[slot].expected) || !same_item(item,committed_.items[slot]))return nullptr;
  const auto now=clock_->now_us();if(now<committed_.observed_us || now-committed_.observed_us>=kLoopMaxGapUs)return nullptr;
  opened_[slot]=true;auto out=std::make_unique<OwnedTransport>();out->backend=std::make_unique<PortProxy>(ports_[slot],clock_);out->transport=std::make_unique<Transport>(*out->backend,*clock_);return out;
}
std::optional<SpiFreshnessProof> NativeDiscoveryExecutor::live_spi_proof(std::size_t slot) {
  if(getpid()!=state_->process || slot>=2 || committed_.items.size()!=2 || !ports_[slot] || !ports_[slot]->active() || committed_.items[slot].identity.transport!="SPI")return std::nullopt;
  std::uint64_t now=0;try{now=clock_->now_us();}catch(...){return std::nullopt;}
  if(now<committed_.observed_us || now-committed_.observed_us>=kLoopMaxGapUs)return std::nullopt;
  return ports_[slot]->live_spi_proof();
}
}  // namespace openpilot::pandad::compatibility
