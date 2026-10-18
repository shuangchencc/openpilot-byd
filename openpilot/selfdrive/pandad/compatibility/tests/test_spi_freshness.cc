#include "../spi_freshness.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <functional>
#include <iostream>
#include <map>
#include <signal.h>
#include <stdexcept>
#include <sys/file.h>
#include <sys/wait.h>
#include <unistd.h>

using namespace openpilot::pandad::compatibility;
using Bytes = std::vector<std::uint8_t>;
extern "C" {
void p10_oracle_exchange(const std::uint8_t *, std::uint8_t *, unsigned int);
void p10_oracle_reset();
unsigned int p10_oracle_controls(unsigned int);
unsigned int p10_oracle_writes();
unsigned int p10_oracle_reads();
void p10_oracle_set_uid_byte(unsigned int, std::uint8_t);
void p10_oracle_set_board(std::uint8_t);
}
namespace {
std::size_t assertions = 0;
void check(bool value, int line) { ++assertions; if (!value) throw std::runtime_error("SPI freshness assertion at " + std::to_string(line)); }
#define CHECK(x) check(static_cast<bool>(x), __LINE__)
std::array<std::uint8_t, 12> uid() { return {1,2,3,4,5,6,7,8,9,10,11,12}; }
class FakeClock final : public Clock {
public:
  std::uint64_t now = 1000;
  std::uint64_t now_us() override { return now; }
  void wait_us(std::uint32_t us) override { now += us; }
};
std::uint32_t le(const Bytes &data, std::size_t offset, unsigned n) {
  std::uint32_t value = 0; for (unsigned i = 0; i < n; ++i) value |= std::uint32_t(data.at(offset + i)) << (8 * i); return value;
}
void append(Bytes &data, std::uint32_t value, unsigned n) { for (unsigned i = 0; i < n; ++i) data.push_back(static_cast<std::uint8_t>(value >> (8 * i))); }
std::uint32_t crc(const Bytes &data) {
  std::uint32_t value = 0xffffffffU; for (auto byte : data) { value ^= byte; for (unsigned bit = 0; bit < 8; ++bit) value = (value >> 1) ^ ((value & 1) ? 0xedb88320U : 0); } return value ^ 0xffffffffU;
}
Bytes wire(std::uint32_t id, std::uint8_t command = 0xc3, std::size_t length = 12) {
  Bytes data{0}; append(data,id,4); append(data,7,2); append(data,static_cast<std::uint32_t>(length+11),2);
  data.push_back(command); append(data,0,2); append(data,0,2); append(data,static_cast<std::uint32_t>(length),2); append(data,crc(data),4); return data;
}
Bytes exchange(const Bytes &tx) { Bytes rx(tx.size()); p10_oracle_exchange(tx.data(),rx.data(),static_cast<unsigned>(tx.size())); return rx; }
void prime(std::uint32_t id, bool consume = true, std::uint8_t command = 0xc3) {
  const auto sent = exchange(wire(id,command)); CHECK(sent[0] == 0x3c);
  if (consume) CHECK(exchange(Bytes(23,0xff))[0] == 0xc3);
}
Request read(std::uint8_t command = 0xd2, std::size_t length = 48) { Request r; r.kind=Kind::ControlRead; r.request=command; r.read_length=length; return r; }
NativeSpiProbePolicy policy(std::uint32_t id = 100) {
  NativeSpiProbePolicy p; p.first_request_id=id; p.selector.path="/dev/spidev0.0";
  p.selector.identity_evidence_sha256=std::string(64,'b'); p.selector.physical_identity_verified=true;
  p.selector.expected_major=153; p.selector.expected_minor=0; p.selector.expected_mode=0;
  p.selector.expected_bits_per_word=8; p.selector.expected_max_speed_hz=10000000; return p;
}
class FakeCalls final : public NativeSpiSyscalls {
public:
  explicit FakeCalls(FakeClock &clock) : clock(clock) {}
  FakeClock &clock;
  int opens=0,closes=0,messages=0,lock_calls=0,unlock_calls=0,holder=-1;
  std::map<int,bool> opened;
  int fail_message=-1,short_message=-1,corrupt_message=-1,wrong_id_message=-1,rollback_message=-1;
  std::uint64_t delay=0;
  std::vector<std::uint32_t> ids;
  NativeSpiCall open_device(const std::string &path,int flags) override {
    CHECK(path=="/dev/spidev0.0" && (flags&O_NOFOLLOW) && (flags&O_CLOEXEC)); const int fd=30+opens++; opened[fd]=true; return {fd,0};
  }
  NativeSpiCall close_device(int fd) override { CHECK(opened[fd] && holder!=fd); opened[fd]=false; ++closes; return {0,0}; }
  NativeSpiCall stat_device(int fd,NativeSpiNode &node) override { CHECK(opened[fd]); node={true,153,0}; return {0,0}; }
  NativeSpiCall read_mode(int fd,std::uint32_t &out) override { CHECK(holder==fd); out=0; return {0,0}; }
  NativeSpiCall read_bits(int fd,std::uint8_t &out) override { CHECK(holder==fd); out=8; return {0,0}; }
  NativeSpiCall read_speed(int fd,std::uint32_t &out) override { CHECK(holder==fd); out=10000000; return {0,0}; }
  NativeSpiCall lock_device(int fd,bool acquire) override {
    CHECK(opened[fd]); if (acquire) { ++lock_calls; if (holder!=-1) return {-1,EWOULDBLOCK}; holder=fd; }
    else { CHECK(holder==fd); holder=-1; ++unlock_calls; } return {0,0};
  }
  NativeSpiCall message(int fd,const std::uint8_t *tx,std::uint8_t *rx,std::size_t size) override {
    CHECK(holder==fd && opened[fd]); ++messages; clock.now+=delay;
    if (messages==rollback_message) clock.now=1;
    if (messages==fail_message) return {-1,EINTR};
    const Bytes bytes(tx,tx+size); if (tx[0]!=0xff) ids.push_back(le(bytes,1,4));
    p10_oracle_exchange(tx,rx,static_cast<unsigned>(size));
    if (messages==corrupt_message && size>11) rx[size-1]^=1;
    if (messages==wrong_id_message && size>11) rx[1]^=1;
    return {static_cast<int>(size)-(messages==short_message ? 1 : 0),0};
  }
};
struct Fixture {
  FakeClock clock;
  std::shared_ptr<FakeCalls> calls=std::make_shared<FakeCalls>(clock);
  NativeSpiProbePolicy options=policy();
  Fixture() { p10_oracle_reset(); }
  std::unique_ptr<SpiFreshnessBackend> backend() { return std::make_unique<SpiFreshnessBackend>(calls,clock); }
  Status activate(SpiFreshnessBackend &backend,std::uint32_t id=100) { return backend.activate(options.selector,uid(),id,100,true,true); }
};
void defaults_inert_and_unknown_ownership_denied() {
  Fixture f; auto backend=f.backend(); CHECK(!backend->proof().synchronized && f.calls->opens==0);
  CHECK(backend->activate(f.options.selector,uid(),100,100)==Status::Invalid);
  CHECK(backend->activate(f.options.selector,uid(),100,100,false,true)==Status::Invalid);
  CHECK(backend->activate(f.options.selector,uid(),100,100,true,false)==Status::Invalid);
  CHECK(!discover_fresh_native_spi(f.clock,f.options,uid()).complete()); CHECK(f.calls->opens==0);
}
void double_uid_fence_and_retained_flock() {
  Fixture f; auto backend=f.backend(); CHECK(f.activate(*backend)==Status::Ok);
  auto proof=backend->proof(); CHECK(proof.synchronized && proof.same_fd_lifetime_lock && !proof.kernel_deadline_proven);
  CHECK(proof.first_fence_id==100 && proof.second_fence_id==101 && proof.expected_uid==uid() && proof.lease_nonce!=0);
  CHECK(p10_oracle_controls(0xc3)==2 && f.calls->messages==5 && f.calls->holder>=0);
  CHECK(f.calls->unlock_calls==1); CHECK(backend->transact(read(),5).status==Status::Ok);
  CHECK(f.calls->ids==std::vector<std::uint32_t>({100,101,102}) && f.calls->unlock_calls==1);
  backend.reset(); CHECK(f.calls->holder==-1 && f.calls->closes==1 && f.calls->unlock_calls==2);
}
void first_uid_cache_hit_second_executes() {
  Fixture f; prime(100); const auto before=p10_oracle_controls(0xc3);
  auto backend=f.backend(); CHECK(f.activate(*backend)==Status::Ok);
  CHECK(p10_oracle_controls(0xc3)==before+1 && backend->proof().synchronized);
}
void prior_second_id_cannot_suppress_second_fence() {
  Fixture f; prime(101); const auto before=p10_oracle_controls(0xc3);
  auto backend=f.backend(); CHECK(f.activate(*backend)==Status::Ok);
  CHECK(p10_oracle_controls(0xc3)==before+2);
}
void queued_and_every_partial_response_boundary() {
  for (unsigned prefix=0;prefix<24;++prefix) {
    Fixture f; prime(100,false);
    if (prefix) CHECK(exchange(Bytes(prefix,0xff))[0]==0xc3);
    const auto before=p10_oracle_controls(0xc3); auto backend=f.backend();
    CHECK(f.activate(*backend)==Status::Ok && p10_oracle_controls(0xc3)==before+1);
    CHECK(p10_oracle_writes()==0 && p10_oracle_reads()==0);
  }
}
void arbitrary_response_lengths_and_cs_boundaries() {
  for(unsigned size : {11U,23U,75U,4096U}) for(unsigned prefix : {0U,1U,6U,7U,size-1,size}) {
    Fixture f;
    Bytes request{0xab}; append(request,42,4); append(request,0,2); append(request,size,2); append(request,crc(request),4);
    CHECK(exchange(request)[0]==0x3c);
    if(prefix) CHECK(exchange(Bytes(prefix,0xff))[0]==0xc3);
    auto backend=f.backend(); CHECK(f.activate(*backend)==Status::Ok);
    CHECK(p10_oracle_controls(0xc3)==2 && p10_oracle_writes()==0 && p10_oracle_reads()==0);
  }
}
void queued_mutation_response_never_replayed() {
  Fixture f; prime(100,false,0xdf); CHECK(p10_oracle_writes()==1);
  auto backend=f.backend(); CHECK(f.activate(*backend)==Status::Ok);
  CHECK(p10_oracle_writes()==1 && p10_oracle_controls(0xc3)==2);
}
void drain_is_invalid_request_when_already_ready() {
  Fixture f; const auto before=p10_oracle_controls(0xc3);
  const auto rx=exchange(Bytes(4096,0xff)); CHECK(rx[0]==0x3c);
  CHECK(p10_oracle_controls(0xc3)==before && p10_oracle_writes()==0 && p10_oracle_reads()==0);
  CHECK(exchange(wire(100))[0]==0x3c); CHECK(exchange(Bytes(23,0xff))[0]==0xc3);
}
void stale_cached_uid_not_accepted_as_current() {
  Fixture f; prime(100); p10_oracle_set_uid_byte(0,77);
  auto backend=f.backend(); CHECK(f.activate(*backend)==Status::IoError);
  CHECK(p10_oracle_controls(0xc3)==2 && !backend->proof().synchronized);
}
void crc_and_wrong_id_reject_fence() {
  for (int message : {3,5}) for (bool corrupt : {false,true}) {
    Fixture f; if(corrupt) f.calls->corrupt_message=message; else f.calls->wrong_id_message=message;
    auto backend=f.backend(); CHECK(f.activate(*backend)==Status::IoError);
    CHECK(!backend->proof().synchronized && f.calls->messages==message);
    const auto before=f.calls->messages; CHECK(backend->transact(read(),5).status==Status::Poisoned && f.calls->messages==before);
  }
}
void uncertain_short_or_interrupted_exchange_never_retried() {
  for(int message=1;message<=5;++message) for(bool shortened:{false,true}) {
    Fixture f; if(shortened) f.calls->short_message=message; else f.calls->fail_message=message;
    auto backend=f.backend(); CHECK(f.activate(*backend)!=Status::Ok && !backend->proof().synchronized);
    CHECK(f.calls->messages==message); const auto before=f.calls->messages;
    CHECK(f.activate(*backend)==Status::Invalid && f.calls->messages==before);
  }
}
void cooperating_process_cannot_interleave_lifetime() {
  Fixture f; auto first=f.backend(); CHECK(f.activate(*first)==Status::Ok);
  auto second=f.backend(); const auto before=f.calls->messages;
  CHECK(f.activate(*second)==Status::Busy && f.calls->messages==before && first->proof().synchronized);
  CHECK(first->transact(read(),5).status==Status::Ok && f.calls->holder==30);
}
void restart_reuses_ids_only_after_new_fence() {
  Fixture f;
  auto first=f.backend(); CHECK(f.activate(*first)==Status::Ok); const auto nonce=first->proof().lease_nonce;
  first.reset(); auto second=f.backend(); CHECK(f.activate(*second)==Status::Ok);
  CHECK(second->proof().lease_nonce!=nonce && p10_oracle_controls(0xc3)==4);
  CHECK(f.calls->ids==std::vector<std::uint32_t>({100,101,100,101}));
}
void crash_pending_request_is_drained_on_restart() {
  Fixture f; auto first=f.backend(); CHECK(f.activate(*first)==Status::Ok);
  f.calls->fail_message=f.calls->messages+2; CHECK(first->transact(read(),5).status==Status::IoError);
  CHECK(!first->proof().synchronized); first.reset(); f.calls->fail_message=-1;
  auto second=f.backend(); CHECK(f.activate(*second)==Status::Ok && second->proof().synchronized);
  CHECK(p10_oracle_controls(0xd2)==1);
}
void cancel_is_local_and_revokes_live_proof() {
  Fixture f; auto backend=f.backend(); CHECK(f.activate(*backend)==Status::Ok);
  const auto before=f.calls->messages, locks=f.calls->lock_calls, unlocks=f.calls->unlock_calls;
  backend->cancel(); CHECK(!backend->proof().synchronized && !backend->proof().same_fd_lifetime_lock);
  CHECK(backend->transact(read(),5).status==Status::Cancelled && f.calls->messages==before);
  CHECK(f.calls->lock_calls==locks && f.calls->unlock_calls==unlocks);
  backend.reset(); CHECK(f.calls->closes==1 && f.calls->holder==-1);
}
void id_exhaustion_refuses_wrap_before_io() {
  Fixture f; auto backend=f.backend(); CHECK(f.activate(*backend,UINT32_MAX-2)==Status::Ok);
  CHECK(backend->transact(read(),5).status==Status::Ok && f.calls->ids.back()==UINT32_MAX);
  const auto before=f.calls->messages; CHECK(backend->transact(read(),5).status==Status::Poisoned);
  CHECK(f.calls->messages==before && !backend->proof().synchronized);
}
void invalid_seed_config_or_identity_never_opens() {
  for(unsigned variant=0;variant<8;++variant) {
    Fixture f; auto backend=f.backend(); auto expected=uid(); auto id=100U;
    if(variant==0) id=0; if(variant==1) id=UINT32_MAX; if(variant==2) id=UINT32_MAX-1;
    if(variant==3) expected.fill(0); if(variant==4) expected.fill(0xff);
    if(variant==5) f.options.selector.expected_mode=1; if(variant==6) f.options.selector.expected_bits_per_word=16;
    if(variant==7) f.options.selector.physical_identity_verified=false;
    CHECK(backend->activate(f.options.selector,expected,id,100,true,true)==Status::Invalid && f.calls->opens==0);
  }
}
void clock_timeout_rollback_fail_closed() {
  for(bool rollback:{false,true}) {
    Fixture f; if(rollback) f.calls->rollback_message=2; else f.calls->delay=5000;
    auto backend=f.backend(); CHECK(f.activate(*backend)!=Status::Ok && !backend->proof().synchronized);
    CHECK(f.calls->messages==(rollback?2:1));
  }
}
void runtime_reads_fail_closed_after_wire_error() {
  Fixture f; auto backend=f.backend(); CHECK(f.activate(*backend)==Status::Ok);
  f.calls->corrupt_message=f.calls->messages+2;
  CHECK(backend->transact(read(),5).status==Status::IoError && !backend->proof().synchronized);
  const auto before=f.calls->messages; CHECK(backend->transact(read(),5).status==Status::Poisoned && f.calls->messages==before);
}
void all_mutations_blocked_before_wire() {
  Fixture f; auto backend=f.backend(); CHECK(f.activate(*backend)==Status::Ok); const auto before=f.calls->messages;
  for(auto kind:{Kind::ControlWrite,Kind::BulkWrite}) { Request request; request.kind=kind; request.request=0xdc; request.endpoint=kind==Kind::BulkWrite?3:0; request.payload=kind==Kind::BulkWrite?Bytes{1}:Bytes{}; CHECK(backend->transact(request,5).status==Status::Invalid); }
  for(auto command:{0xdf,0xdc,0xd8,0xd1,0xd5,0xff}) CHECK(backend->transact(read(command,16),5).status==Status::Invalid);
  CHECK(f.calls->messages==before && p10_oracle_writes()==0);
}
void fresh_probe_same_owner_nonce_and_monotonic_ids() {
  Fixture f; auto result=discover_fresh_native_spi(f.calls,f.clock,f.options,uid(),true,true);
  CHECK(result.complete() && result.probe.observation.uid==uid() && result.probe.observation.board_type==7);
  CHECK(result.probe.observation.lease_nonce==result.freshness.lease_nonce && result.freshness.expected_uid==uid());
  auto *backend=dynamic_cast<SpiFreshnessBackend *>(result.probe.transport->backend.get()); CHECK(backend && backend->proof().synchronized);
  CHECK(f.calls->ids==std::vector<std::uint32_t>({100,101,102,103,104,105,106}));
  CHECK(result.probe.transport->transport->perform(read(),5).status==Status::Ok && f.calls->ids.back()==107);
  CHECK(f.calls->unlock_calls==1); result.probe.transport->transport->cancel(); CHECK(!backend->proof().synchronized);
}
void fresh_probe_f4_allowlist_and_signature() {
  Fixture f; p10_oracle_set_board(6); std::array<std::uint8_t,128> signature{};
  for(std::size_t i=0;i<signature.size();++i) signature[i]=static_cast<std::uint8_t>(i+17);
  f.options.f4_application_signatures={signature}; auto result=discover_fresh_native_spi(f.calls,f.clock,f.options,uid(),true,true);
  CHECK(result.complete() && result.probe.observation.f4_capabilities.size()==16 && p10_oracle_controls(0xd5)==1);
  CHECK(p10_oracle_writes()==0 && result.probe.first_request_id==100);
}
void actual_posix_advisory_lock_process_exclusion() {
  // A temporary ordinary file exercises the native flock primitive and process
  // lifetime only. It is not a device, and makes no claim about other masters.
  char path[]="/tmp/songplus-p10-flock-XXXXXX"; const int fd=mkstemp(path); CHECK(fd>=0);
  CHECK(flock(fd,LOCK_EX|LOCK_NB)==0);
  const pid_t child=fork(); CHECK(child>=0);
  if(child==0) {
    const int competing=open(path,O_RDWR|O_CLOEXEC); if(competing<0) _exit(2);
    const int result=flock(competing,LOCK_EX|LOCK_NB); const int error=errno;
    close(competing); close(fd); _exit(result==-1&&(error==EWOULDBLOCK||error==EAGAIN)?0:3);
  }
  int status=0; CHECK(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
  CHECK(flock(fd,LOCK_UN)==0); const int successor=open(path,O_RDWR|O_CLOEXEC); CHECK(successor>=0);
  CHECK(flock(successor,LOCK_EX|LOCK_NB)==0); CHECK(close(successor)==0 && close(fd)==0 && unlink(path)==0);
}
class RealLockFakeDevice final : public NativeSpiSyscalls {
public:
  explicit RealLockFakeDevice(std::string path) : path_(std::move(path)) {}
  int messages=0;
  NativeSpiCall open_device(const std::string &,int flags) override { return result(open(path_.c_str(),flags)); }
  NativeSpiCall close_device(int fd) override { return result(close(fd)); }
  NativeSpiCall stat_device(int,NativeSpiNode &node) override { node={true,153,0}; return {0,0}; }
  NativeSpiCall read_mode(int,std::uint32_t &out) override { out=0; return {0,0}; }
  NativeSpiCall read_bits(int,std::uint8_t &out) override { out=8; return {0,0}; }
  NativeSpiCall read_speed(int,std::uint32_t &out) override { out=10000000; return {0,0}; }
  NativeSpiCall lock_device(int fd,bool acquire) override { return result(flock(fd,acquire?LOCK_EX|LOCK_NB:LOCK_UN)); }
  NativeSpiCall message(int,const std::uint8_t *tx,std::uint8_t *rx,std::size_t size) override {
    ++messages; p10_oracle_exchange(tx,rx,static_cast<unsigned>(size)); return {static_cast<int>(size),0};
  }
private:
  static NativeSpiCall result(int value) { return {value,value<0?errno:0}; }
  std::string path_;
};
void fork_cannot_reuse_or_unlock_parent_session() {
  p10_oracle_reset(); FakeClock clock;
  char path[]="/tmp/songplus-p10-fork-XXXXXX"; const int placeholder=mkstemp(path); CHECK(placeholder>=0); CHECK(close(placeholder)==0);
  auto calls=std::make_shared<RealLockFakeDevice>(path); auto backend=std::make_unique<SpiFreshnessBackend>(calls,clock);
  CHECK(backend->activate(policy().selector,uid(),100,100,true,true)==Status::Ok);
  const auto proof=backend->proof(); const auto before=calls->messages;
  const pid_t child=fork(); CHECK(child>=0);
  if(child==0) {
    if(backend->proof().synchronized || backend->proof().same_fd_lifetime_lock) _exit(2);
    if(backend->transact(read(),5).status!=Status::Poisoned || calls->messages!=before) _exit(3);
    if(backend->activate(policy().selector,uid(),100,100,true,true)!=Status::Invalid) _exit(4);
    backend.reset(); // Must close inherited fd WITHOUT LOCK_UN on parent's OFD.
    const int competing=open(path,O_RDWR|O_CLOEXEC); if(competing<0) _exit(5);
    const auto locked=flock(competing,LOCK_EX|LOCK_NB); const auto error=errno; close(competing);
    _exit(locked<0 && (error==EAGAIN || error==EWOULDBLOCK) ? 0 : 6);
  }
  int status=0; CHECK(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
  CHECK(backend->proof().synchronized && backend->proof().lease_nonce==proof.lease_nonce);
  const int competitor=open(path,O_RDWR|O_CLOEXEC); CHECK(competitor>=0);
  CHECK(flock(competitor,LOCK_EX|LOCK_NB)<0 && (errno==EAGAIN || errno==EWOULDBLOCK));
  CHECK(backend->transact(read(),5).status==Status::Ok && calls->messages==before+2);
  backend.reset(); CHECK(flock(competitor,LOCK_EX|LOCK_NB)==0);
  CHECK(close(competitor)==0 && unlink(path)==0);
}
void crashed_process_releases_advisory_lock() {
  char path[]="/tmp/songplus-p10-crash-XXXXXX"; const int fd=mkstemp(path); CHECK(fd>=0);
  int ready[2]; CHECK(pipe(ready)==0); const pid_t child=fork(); CHECK(child>=0);
  if(child==0) {
    close(ready[0]); close(fd);
    const int own=open(path,O_RDWR|O_CLOEXEC); if(own<0 || flock(own,LOCK_EX|LOCK_NB)!=0) _exit(2);
    const char value='L'; if(write(ready[1],&value,1)!=1) _exit(3);
    for(;;) pause();
  }
  close(ready[1]); char value=0; CHECK(::read(ready[0],&value,1)==1 && value=='L'); close(ready[0]);
  CHECK(flock(fd,LOCK_EX|LOCK_NB)==-1 && (errno==EWOULDBLOCK || errno==EAGAIN));
  CHECK(kill(child,SIGKILL)==0); int status=0;
  CHECK(waitpid(child,&status,0)==child && WIFSIGNALED(status) && WTERMSIG(status)==SIGKILL);
  CHECK(flock(fd,LOCK_EX|LOCK_NB)==0); CHECK(close(fd)==0 && unlink(path)==0);
}
}  // namespace
int main() {
  try {
    const std::vector<std::pair<const char *,std::function<void()>>> groups{
      {"defaults_inert_and_unknown_ownership_denied",defaults_inert_and_unknown_ownership_denied},
      {"double_uid_fence_and_retained_flock",double_uid_fence_and_retained_flock},
      {"first_uid_cache_hit_second_executes",first_uid_cache_hit_second_executes},
      {"prior_second_id_cannot_suppress_second_fence",prior_second_id_cannot_suppress_second_fence},
      {"queued_and_every_partial_response_boundary",queued_and_every_partial_response_boundary},
      {"arbitrary_response_lengths_and_cs_boundaries",arbitrary_response_lengths_and_cs_boundaries},
      {"queued_mutation_response_never_replayed",queued_mutation_response_never_replayed},
      {"drain_is_invalid_request_when_already_ready",drain_is_invalid_request_when_already_ready},
      {"stale_cached_uid_not_accepted_as_current",stale_cached_uid_not_accepted_as_current},
      {"crc_and_wrong_id_reject_fence",crc_and_wrong_id_reject_fence},
      {"uncertain_short_or_interrupted_exchange_never_retried",uncertain_short_or_interrupted_exchange_never_retried},
      {"cooperating_process_cannot_interleave_lifetime",cooperating_process_cannot_interleave_lifetime},
      {"restart_reuses_ids_only_after_new_fence",restart_reuses_ids_only_after_new_fence},
      {"crash_pending_request_is_drained_on_restart",crash_pending_request_is_drained_on_restart},
      {"cancel_is_local_and_revokes_live_proof",cancel_is_local_and_revokes_live_proof},
      {"id_exhaustion_refuses_wrap_before_io",id_exhaustion_refuses_wrap_before_io},
      {"invalid_seed_config_or_identity_never_opens",invalid_seed_config_or_identity_never_opens},
      {"clock_timeout_rollback_fail_closed",clock_timeout_rollback_fail_closed},
      {"runtime_reads_fail_closed_after_wire_error",runtime_reads_fail_closed_after_wire_error},
      {"all_mutations_blocked_before_wire",all_mutations_blocked_before_wire},
      {"fresh_probe_same_owner_nonce_and_monotonic_ids",fresh_probe_same_owner_nonce_and_monotonic_ids},
      {"fresh_probe_f4_allowlist_and_signature",fresh_probe_f4_allowlist_and_signature},
      {"actual_posix_advisory_lock_process_exclusion",actual_posix_advisory_lock_process_exclusion},
      {"crashed_process_releases_advisory_lock",crashed_process_releases_advisory_lock},
      {"fork_cannot_reuse_or_unlock_parent_session",fork_cannot_reuse_or_unlock_parent_session},
    };
    for(const auto &[name,test]:groups) { test(); std::cout<<"PASS "<<name<<'\n'; }
    std::cout<<"{\"suite\":\"p10_spi_freshness\",\"passed\":"<<groups.size()<<",\"failed\":0,\"skipped\":0,\"assertions\":"<<assertions<<",\"real_device_io\":false}\n"; return 0;
  } catch(const std::exception &error) { std::cerr<<error.what()<<'\n'; return 1; }
}
