#include "../spi_backend.h"
#include "../can_codec.h"

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace openpilot::pandad::compatibility;
namespace {
using Bytes=std::vector<std::uint8_t>;
std::size_t assertions=0;
void check(bool ok,int line) { ++assertions; if (!ok) throw std::runtime_error("P6 single SPI assertion line "+std::to_string(line)); }
#define CHECK(x) check(static_cast<bool>(x),__LINE__)
std::uint32_t le(const Bytes &b,std::size_t n,unsigned width) {
  std::uint32_t out=0; for (unsigned i=0;i<width;++i) out|=static_cast<std::uint32_t>(b.at(n+i))<<(i*8); return out;
}
void append(Bytes &b,std::uint32_t value,unsigned width) { for (unsigned i=0;i<width;++i) b.push_back(value>>(i*8)); }
std::uint32_t crc(const Bytes &b) {
  std::uint32_t out=0xffffffffU;
  for (const auto byte:b) { out^=byte; for (unsigned i=0;i<8;++i) out=(out>>1)^((out&1U)?0xedb88320U:0); }
  return out^0xffffffffU;
}
struct FakeClock final : Clock {
  std::uint64_t now=1000000;
  std::uint64_t now_us() override { return now; }
  void wait_us(std::uint32_t us) override { now+=us; }
};
struct H7Spi final : SpiIo {
  std::vector<Bytes> requests;
  Bytes reply,rx;
  std::uint32_t id=0;
  unsigned exchanges=0,begins=0,ends=0;
  bool lease=false,bad_crc=false,nack=false;
  Status begin(std::uint32_t ms) override { CHECK(ms>0 && ms<=5); ++begins; lease=true; return Status::Ok; }
  void end() noexcept override { ++ends; lease=false; }
  Result exchange(const Bytes &tx,std::uint32_t ms) override {
    CHECK(lease && ms>0 && ms<=5); ++exchanges;
    if (tx[0]!=0xff) {
      CHECK(tx.size()>=13); CHECK(le(tx,tx.size()-4,4)==crc(Bytes(tx.begin(),tx.end()-4)));
      CHECK(le(tx,5,2)+13==tx.size()); requests.push_back(tx); id=le(tx,1,4); reply.clear();
      if (tx[0]==0) {
        CHECK(le(tx,5,2)==7); const auto request=tx[9]; CHECK(request!=0xd5);
        const auto length=le(tx,14,2); reply.resize(length);
        if (request==0xdd) { CHECK(length==8); reply={0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75}; }
        else if (request==0xd2) {
          CHECK(length==48); reply[4]=0xe0; reply[5]=0x2e; reply[6]=0xa4; reply[7]=1;
          reply[28]=1; reply[30]=1; reply[31]=19; reply[45]=7; reply[47]=75;
        } else if (request==0xc2) { CHECK(length==64); reply[45]=reply[46]=1; }
        else { CHECK(request==0xdc || request==0xf3); CHECK(length==0); if(request==0xdc) CHECK(le(tx,10,2)==19); }
      } else if (tx[0]==0x81) { reply=rx; rx.clear(); }
      else CHECK(tx[0]==3);
      Bytes ready(tx.size()); ready[0]=0x3c; return {Status::Ok,ready.size(),ready};
    }
    Bytes out{static_cast<std::uint8_t>(nack?0x5a:0xc3)}; append(out,id,4);
    const Bytes empty; const auto &payload=nack?empty:reply;
    append(out,payload.size(),2); out.insert(out.end(),payload.begin(),payload.end());
    append(out,crc(out)^(bad_crc?1U:0U),4); CHECK(out.size()<=tx.size()); out.resize(tx.size());
    return {Status::Ok,out.size(),out};
  }
};
Request control(unsigned code,std::size_t length=0,unsigned value=0) {
  Request r; r.kind=length?Kind::ControlRead:Kind::ControlWrite; r.request=code; r.read_length=length; r.value=value; return r;
}
Request txframe(bool fd) {
  CanFrame f{0x123,2,false,fd,false,false,Bytes(fd?64:8,0x55)};
  const auto encoded=encode_can_frame(f,CanCapability::CanFd,FrameDirection::Tx); CHECK(encoded);
  Request r; r.kind=Kind::BulkWrite; r.endpoint=3; r.payload=encoded.value; return r;
}
struct Rig { FakeClock clock; H7Spi io; SpiBackend spi{io,clock,0x76543210}; Transport transport{spi,clock}; };
}
int main() {
  unsigned passed=0;
  const auto pass=[&](const char *name) { ++passed; std::cout<<"PASS "<<name<<'\n'; };
  try {
    {
      Rig r; CHECK(r.io.exchanges==0);
      const auto versions=r.transport.perform(control(0xdd,8),5);
      CHECK(versions.status==Status::Ok && versions.data==Bytes({0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75}));
      const auto health=r.transport.perform(control(0xd2,48),5); CHECK(health.status==Status::Ok && health.actual==48);
      CHECK(le(health.data,6,2)==420 && le(health.data,45,2)==7);
      const auto can=r.transport.perform(control(0xc2,64),5); CHECK(can.status==Status::Ok && can.data[45]==1 && can.data[46]==1);
      CHECK(r.transport.perform(control(0xdc,0,19),5).status==Status::Ok);
      CHECK(r.transport.perform(control(0xf3,0,1),5).status==Status::Ok);
      CHECK(r.io.requests.size()==5 && r.io.begins==5 && r.io.ends==5);
      for (const auto &wire:r.io.requests) CHECK(wire[9]!=0xd5);
      pass("p6_single_h7_spi_current_protocol_health_safety_heartbeat_no_f4_handshake");
    }
    {
      Rig r;
      for (bool fd:{false,true}) {
        const auto request=txframe(fd); const auto sent=r.transport.perform(request,5);
        CHECK(sent.status==Status::Ok && sent.actual==request.payload.size());
        CanFrame returned{0x123,2,false,fd,true,false,Bytes(fd?64:8,0x55)};
        const auto encoded=encode_can_frame(returned,CanCapability::CanFd,FrameDirection::Rx); CHECK(encoded); r.io.rx=encoded.value;
        Request read; read.kind=Kind::BulkRead; read.endpoint=0x81; read.read_length=100;
        const auto received=r.transport.perform(read,5); CHECK(received.status==Status::Short);
        CanStreamParser parser(CanCapability::CanFd); const auto frames=parser.feed(received.data.data(),received.data.size());
        CHECK(frames && frames.value.size()==1); CHECK(frames.value[0].returned && frames.value[0].fd==fd);
        CHECK(frames.value[0].local_bus==2 && frames.value[0].payload==returned.payload);
        CHECK(r.io.requests[r.io.requests.size()-2][0]==3);
      }
      CHECK(r.io.requests.size()==4); CHECK(r.transport.healthy());
      // This is one standalone SPI session: no dual-board profile, group gate,
      // slot promotion, USB API, opener or Panda constructor participates.
      pass("p6_single_h7_spi_classic_and_64byte_fd_bidirectional_positive");
    }
    {
      Rig r; r.io.nack=true; const auto result=r.transport.perform(txframe(true),5);
      CHECK(result.status==Status::Busy && result.actual==0 && result.delivery_uncertain);
      CHECK(r.io.requests.size()==1 && r.io.exchanges==2); CHECK(!r.transport.healthy());
      CHECK(r.transport.perform(txframe(true),5).status==Status::Poisoned); CHECK(r.io.requests.size()==1);
      pass("p6_single_h7_spi_nack_never_replayed_or_switched_to_usb");
    }
    {
      Rig r; r.io.bad_crc=true;
      const auto result=r.transport.perform(control(0xdd,8),5); CHECK(result.status==Status::IoError && !result.actual_known);
      CHECK(!r.transport.healthy()); CHECK(r.io.requests.size()==1);
      r.io.bad_crc=false; CHECK(r.transport.perform(control(0xdd,8),5).status==Status::Poisoned); CHECK(r.io.requests.size()==1);
      pass("p6_single_h7_spi_bad_crc_requires_new_session");
    }
    {
      Rig r; r.transport.cancel(); CHECK(r.transport.perform(control(0xdd,8),5).status==Status::Cancelled);
      CHECK(r.io.begins==0 && r.io.exchanges==0);
      pass("p6_single_h7_spi_cancel_is_local_and_inert");
    }
    std::cout<<"{\"suite\":\"p6_single_h7_spi\",\"passed\":"<<passed<<",\"failed\":0,\"skipped\":0,\"assertions\":"<<assertions
      <<",\"standalone_sessions\":1,\"dual_profile_used\":false,\"device_access\":false,\"real_can_tx\":false,\"legacy_device_path_executed\":false}\n";
  } catch (const std::exception &e) { std::cerr<<e.what()<<'\n'; return 1; }
}
