#pragma once

// Host-only adapter around the actual locked firmware oracle linked by the P10
// runner. One SPI board per fixture; fake syscalls never open a physical node.
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <functional>
#include <map>
#include <memory>
#include <stdexcept>

#include "../native_spi.h"
#include "../native_usb.h"

extern "C" {
void p10_oracle_exchange(const std::uint8_t *, std::uint8_t *, unsigned int);
void p10_oracle_reset();
void p10_oracle_set_uid_byte(unsigned int, std::uint8_t);
void p10_oracle_set_board(std::uint8_t);
unsigned int p10_oracle_controls(unsigned int);
unsigned int p10_oracle_writes();
}
namespace p10_spi_fixture {
using namespace openpilot::pandad::compatibility;
inline void require(bool condition) { if (!condition) throw std::runtime_error("P10 SPI engine fixture invariant"); }
inline NativeUsbObservation observation() {
  NativeUsbObservation value;
  value.uid={1,2,3,4,5,6,7,8,9,10,11,12}; value.board_type=7;
  value.protocol={0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75};
  for (std::size_t i=0;i<value.application_signature.size();++i) value.application_signature[i]=static_cast<std::uint8_t>(i+17);
  return value;
}
inline NativeSpiSelector selector() {
  NativeSpiSelector value; value.path="/dev/spidev0.0";
  value.identity_evidence_sha256=std::string(64,'b'); value.physical_identity_verified=true;
  value.expected_major=153; value.expected_minor=0; value.expected_mode=0;
  value.expected_bits_per_word=8; value.expected_max_speed_hz=10000000;
  return value;
}
struct State {
  std::atomic<unsigned> opens{0}, closes{0}, messages{0}, locks{0}, unlocks{0};
  std::atomic<bool> fail_next_message{false};
  std::function<void()> on_message;
  int held_fd=-1;
  std::map<int,bool> opened;
  State() { p10_oracle_reset(); }
  // Call while no I/O is in flight; changing UID simulates replacement/drift.
  void set_uid(const std::array<std::uint8_t,12> &uid) {
    for(unsigned i=0;i<uid.size();++i)p10_oracle_set_uid_byte(i,uid[i]);
  }
};
class Calls final : public NativeSpiSyscalls {
public:
  explicit Calls(std::shared_ptr<State> state):state_(std::move(state)){}
  NativeSpiCall open_device(const std::string &path,int) override {
    require(path==selector().path); const int fd=60+static_cast<int>(state_->opens++); state_->opened[fd]=true; return {fd,0};
  }
  NativeSpiCall close_device(int fd) override {
    require(state_->opened[fd] && state_->held_fd!=fd); state_->opened[fd]=false; ++state_->closes; return {0,0};
  }
  NativeSpiCall stat_device(int fd,NativeSpiNode &node) override {
    require(state_->opened[fd]); node={true,153,0}; return {0,0};
  }
  NativeSpiCall read_mode(int fd,std::uint32_t &out) override {require(state_->held_fd==fd);out=0;return {0,0};}
  NativeSpiCall read_bits(int fd,std::uint8_t &out) override {require(state_->held_fd==fd);out=8;return {0,0};}
  NativeSpiCall read_speed(int fd,std::uint32_t &out) override {require(state_->held_fd==fd);out=10000000;return {0,0};}
  NativeSpiCall lock_device(int fd,bool acquire) override {
    require(state_->opened[fd]);
    if(acquire) {++state_->locks;if(state_->held_fd!=-1)return {-1,EWOULDBLOCK};state_->held_fd=fd;}
    else {require(state_->held_fd==fd);state_->held_fd=-1;++state_->unlocks;}
    return {0,0};
  }
  NativeSpiCall message(int fd,const std::uint8_t *tx,std::uint8_t *rx,std::size_t size) override {
    require(state_->opened[fd] && state_->held_fd==fd); ++state_->messages;
    if(state_->on_message)state_->on_message();
    if(state_->fail_next_message.exchange(false))return {-1,EINTR};
    p10_oracle_exchange(tx,rx,static_cast<unsigned>(size));return {static_cast<int>(size),0};
  }
private:
  std::shared_ptr<State> state_;
};
inline std::shared_ptr<NativeSpiSyscalls> calls(const std::shared_ptr<State> &state) { return std::make_shared<Calls>(state); }
}  // namespace p10_spi_fixture
