#include "../can_codec.h"

#include <algorithm>
#include <climits>
#include <cstdlib>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

using namespace openpilot::pandad::compatibility;
using Bytes = std::vector<std::uint8_t>;

namespace {

std::size_t assertions = 0;
void check(bool value, int line) {
  ++assertions;
  if (!value) throw std::runtime_error("assertion at line " + std::to_string(line));
}
#define CHECK(value) check(static_cast<bool>(value), __LINE__)

Bytes unhex(const std::string &text) {
  if (text == "-") return {};
  if (text.size() % 2 != 0) throw std::invalid_argument("odd hex length");
  Bytes result;
  const auto nibble = [](char c) -> unsigned {
    if (c >= '0' && c <= '9') return static_cast<unsigned>(c - '0');
    if (c >= 'a' && c <= 'f') return static_cast<unsigned>(c - 'a' + 10);
    if (c >= 'A' && c <= 'F') return static_cast<unsigned>(c - 'A' + 10);
    throw std::invalid_argument("bad hex");
  };
  for (std::size_t i = 0; i < text.size(); i += 2) result.push_back(static_cast<std::uint8_t>((nibble(text[i]) << 4U) | nibble(text[i + 1])));
  return result;
}

std::string hex(const Bytes &bytes) {
  std::ostringstream stream;
  stream << std::hex << std::setfill('0');
  for (const auto byte : bytes) stream << std::setw(2) << static_cast<unsigned>(byte);
  return stream.str();
}

bool same_frame(const CanFrame &left, const CanFrame &right) {
  return left.address == right.address && left.local_bus == right.local_bus && left.extended == right.extended &&
         left.fd == right.fd && left.returned == right.returned && left.rejected == right.rejected && left.payload == right.payload;
}

void fix_checksum(Bytes &bytes) {
  bytes[5] = 0;
  std::uint8_t sum = 0;
  for (auto b : bytes) sum ^= b;
  bytes[5] = sum;
}

struct Golden { const char *name; CanFrame frame; const char *wire; };
const std::vector<Golden> goldens = {
  {"empty", {0, 0, false, false, false, false, {}}, "000000000000"},
  {"standard_bus1", {0x123, 1, false, false, false, false, {1, 2, 3}}, "321809000023010203"},
  {"standard_max_bus2", {0x7ff, 2, false, false, false, false, {0, 1, 2, 3, 4, 5, 6, 7}}, "84f83f0000430001020304050607"},
  {"extended_max_fd_returned", {0x1fffffff, 0, true, true, true, false, {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}},
   "91feffffff90000102030405060708090a0b"},
  {"extended_rejected", {0x800, 2, true, false, false, true, {}}, "040540000041"},
  {"extended_zero_short_fd", {0, 0, true, true, false, false, {}}, "010400000005"},
};

void print_frame_fields(const CanFrame &frame) {
  std::cout << "\"address\":" << frame.address << ",\"local_bus\":" << static_cast<unsigned>(frame.local_bus)
            << ",\"extended\":" << (frame.extended ? "true" : "false")
            << ",\"fd\":" << (frame.fd ? "true" : "false")
            << ",\"returned\":" << (frame.returned ? "true" : "false")
            << ",\"rejected\":" << (frame.rejected ? "true" : "false")
            << ",\"payload_hex\":\"" << hex(frame.payload) << "\"";
}

CanCapability capability_arg(const std::string &arg) {
  if (arg == "f4") return CanCapability::ClassicOnly;
  if (arg == "h7") return CanCapability::CanFd;
  throw std::invalid_argument("capability must be f4 or h7");
}

FrameDirection direction_arg(const std::string &arg) {
  if (arg == "rx") return FrameDirection::Rx;
  if (arg == "tx") return FrameDirection::Tx;
  throw std::invalid_argument("direction must be rx or tx");
}

std::uint64_t uint64_arg(const std::string &arg) {
  if (arg.empty() || !std::all_of(arg.begin(), arg.end(), [](char c) { return c >= '0' && c <= '9'; })) throw std::invalid_argument("not unsigned decimal");
  return std::stoull(arg);
}

std::uint32_t uint_arg(const std::string &arg) {
  const auto value = uint64_arg(arg);
  if (value > std::numeric_limits<std::uint32_t>::max()) throw std::invalid_argument("uint32 overflow");
  return static_cast<std::uint32_t>(value);
}

bool bool_arg(const std::string &arg) {
  if (arg == "0") return false;
  if (arg == "1") return true;
  throw std::invalid_argument("not a bool");
}

int cli(int argc, char **argv) {
  if (argc == 2 && std::string(argv[1]) == "--vectors") {
    std::cout << '[';
    bool comma = false;
    for (const auto &golden : goldens) {
      if (comma) std::cout << ',';
      std::cout << "{\"name\":\"" << golden.name << "\",\"wire_hex\":\"" << golden.wire << "\",";
      print_frame_fields(golden.frame);
      std::cout << '}';
      comma = true;
    }
    std::cout << "]\n";
    return 0;
  }
  if (argc == 5 && std::string(argv[1]) == "--decode") {
    const auto bytes = unhex(argv[4]);
    const auto decoded = decode_can_frame(bytes.data(), bytes.size(), capability_arg(argv[2]), direction_arg(argv[3]));
    if (!decoded) {
      std::cout << "{\"ok\":false,\"error\":\"" << codec_error_name(decoded.error) << "\"}\n";
      return 1;
    }
    std::cout << "{\"ok\":true,";
    print_frame_fields(decoded.value);
    std::cout << "}\n";
    return 0;
  }
  if (argc == 11 && std::string(argv[1]) == "--encode") {
    const auto bus = uint_arg(argv[5]);
    CanFrame frame{uint64_arg(argv[4]), bus, bool_arg(argv[6]), bool_arg(argv[7]),
                   bool_arg(argv[8]), bool_arg(argv[9]), unhex(argv[10])};
    const auto encoded = encode_can_frame(frame, capability_arg(argv[2]), direction_arg(argv[3]));
    if (!encoded) {
      std::cout << "{\"ok\":false,\"error\":\"" << codec_error_name(encoded.error) << "\"}\n";
      return 1;
    }
    std::cout << "{\"ok\":true,\"wire_hex\":\"" << hex(encoded.value) << "\"}\n";
    return 0;
  }
  throw std::invalid_argument("unknown CLI arguments");
}

}  // namespace

int main(int argc, char **argv) {
  if (argc == 2 && std::string(argv[1]) == "--batch") {
    std::string line;
    while (std::getline(std::cin, line)) {
      try {
        if (line.size() > 32768) throw std::invalid_argument("batch line exceeds size limit");
        std::istringstream input(line);
        std::vector<std::string> args{"test_can_codec"};
        for (std::string token; input >> token;) {
          if (args.size() >= 11) throw std::invalid_argument("too many batch arguments");
          args.push_back(std::move(token));
        }
        std::vector<char *> pointers;
        for (auto &arg : args) pointers.push_back(arg.data());
        (void)cli(static_cast<int>(pointers.size()), pointers.data());
      } catch (const std::exception &) {
        std::cout << "{\"ok\":false,\"error\":\"invalid_cli_argument\"}\n";
      }
    }
    return 0;
  }
  if (argc > 1) {
    try { return cli(argc, argv); }
    catch (const std::exception &) { std::cout << "{\"ok\":false,\"error\":\"invalid_cli_argument\"}\n"; return 2; }
  }
  std::size_t groups = 0;
  const auto test = [&groups](const char *name, const std::function<void()> &run) {
    run();
    ++groups;
    std::cout << "PASS " << name << '\n';
  };
  try {
    test("fixed_independent_golden_vectors", [] {
      for (const auto &golden : goldens) {
        const auto bytes = unhex(golden.wire);
        const auto encoded = encode_can_frame(golden.frame, CanCapability::CanFd, FrameDirection::Rx);
        CHECK(encoded); CHECK(encoded.value == bytes);
        const auto decoded = decode_can_frame(bytes.data(), bytes.size(), CanCapability::CanFd, FrameDirection::Rx);
        CHECK(decoded); CHECK(same_frame(golden.frame, decoded.value));
      }
    });
    test("all_16_dlc_and_classic_lengths", [] {
      for (std::size_t dlc = 0; dlc < 16; ++dlc) {
        CanFrame frame{0, 0, false, true, false, false, Bytes(kCanDlcLengths[dlc], 0)};
        Bytes expected(6 + kCanDlcLengths[dlc], 0);
        expected[0] = static_cast<std::uint8_t>((dlc << 4U) | 1U); expected[5] = expected[0];
        auto encoded = encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Tx);
        CHECK(encoded); CHECK(encoded.value == expected);
        auto decoded = decode_can_frame(expected.data(), expected.size(), CanCapability::CanFd, FrameDirection::Rx);
        CHECK(decoded); CHECK(same_frame(decoded.value, frame));
        CHECK(encode_can_frame(frame, CanCapability::ClassicOnly, FrameDirection::Tx).error == CodecError::UnsupportedFd);
        CHECK(decode_can_frame(expected.data(), expected.size(), CanCapability::ClassicOnly, FrameDirection::Rx).error == CodecError::UnsupportedFd);
        frame.fd = false;
        expected[0] &= 0xfeU; expected[5] = expected[0];
        encoded = encode_can_frame(frame, CanCapability::ClassicOnly, FrameDirection::Tx);
        decoded = decode_can_frame(expected.data(), expected.size(), CanCapability::CanFd, FrameDirection::Rx);
        CHECK(static_cast<bool>(encoded) == (dlc <= 8)); CHECK(static_cast<bool>(decoded) == (dlc <= 8));
        if (dlc <= 8) CHECK(encoded.value == expected);
      }
      for (std::size_t length : {9, 10, 11, 13, 15, 17, 21, 25, 33, 49, 63, 65, 1024}) {
        CanFrame frame{0, 0, false, true, false, false, Bytes(length)};
        CHECK(encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Tx).error == CodecError::InvalidLength);
      }
    });
    test("address_boundaries", [] {
      for (const bool extended : {false, true}) {
        const std::uint32_t maximum = extended ? 0x1fffffffU : 0x7ffU;
        for (const auto address : {0U, maximum - 1U, maximum}) {
          CanFrame frame{address, 0, extended, false, false, false, {}};
          CHECK(encode_can_frame(frame, CanCapability::ClassicOnly, FrameDirection::Tx));
        }
        for (const auto address : {maximum + 1U, UINT32_MAX}) {
          CanFrame frame{address, 0, extended, false, false, false, {}};
          CHECK(encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Tx).error == CodecError::InvalidAddress);
        }
      }
      auto invalid = unhex("000040000040");  // standard address0x800, valid XOR
      CHECK(decode_can_frame(invalid.data(), invalid.size(), CanCapability::CanFd, FrameDirection::Rx).error == CodecError::InvalidAddress);
      for (const auto address : {UINT64_C(0x100000000), UINT64_MAX}) {
        CanFrame frame{address, 0, true, false, false, false, {}};
        CHECK(encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Tx).error == CodecError::InvalidAddress);
      }
    });
    test("flags_buses_checksum_and_exact_size", [] {
      for (unsigned bus = 3; bus < 256; ++bus) {
        CanFrame frame{0, bus, false, false, false, false, {}};
        CHECK(encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Rx).error == CodecError::InvalidBus);
        if (bus < 8) {
          Bytes bytes{static_cast<std::uint8_t>(bus << 1U), 0, 0, 0, 0, 0}; fix_checksum(bytes);
          CHECK(decode_can_frame(bytes.data(), bytes.size(), CanCapability::CanFd, FrameDirection::Rx).error == CodecError::InvalidBus);
        }
      }
      for (const auto bus : {256U, 257U, 258U, UINT32_MAX}) {
        CanFrame frame{0, bus, false, false, false, false, {}};
        CHECK(encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Tx).error == CodecError::InvalidBus);
      }
      for (const bool returned : {false, true}) for (const bool rejected : {false, true}) {
        CanFrame frame{0, 0, false, false, returned, rejected, {}};
        CHECK(static_cast<bool>(encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Rx)) == !(returned && rejected));
        CHECK(static_cast<bool>(encode_can_frame(frame, CanCapability::CanFd, FrameDirection::Tx)) == !(returned || rejected));
        Bytes bytes{0, static_cast<std::uint8_t>((returned ? 2 : 0) | (rejected ? 1 : 0)), 0, 0, 0, 0}; fix_checksum(bytes);
        CHECK(static_cast<bool>(decode_can_frame(bytes.data(), bytes.size(), CanCapability::CanFd, FrameDirection::Rx)) == !(returned && rejected));
        CHECK(static_cast<bool>(decode_can_frame(bytes.data(), bytes.size(), CanCapability::CanFd, FrameDirection::Tx)) == !(returned || rejected));
      }
      const auto valid = unhex(goldens[2].wire);
      for (std::size_t i = 0; i < valid.size(); ++i) {
        auto corrupt = valid; corrupt[i] ^= 1U;
        CHECK(!decode_can_frame(corrupt.data(), corrupt.size(), CanCapability::CanFd, FrameDirection::Rx));
      }
      auto trailing = valid; trailing.push_back(0);
      CHECK(decode_can_frame(trailing.data(), trailing.size(), CanCapability::CanFd, FrameDirection::Rx).error == CodecError::InvalidLength);
      Bytes overlong(71, 0);
      CHECK(decode_can_frame(overlong.data(), overlong.size(), CanCapability::CanFd, FrameDirection::Rx).error == CodecError::InvalidLength);
    });
    test("every_short_header_prefix", [] {
      const auto bytes = unhex(goldens[2].wire);
      for (std::size_t size = 0; size < 6; ++size) {
        CHECK(decode_can_frame(bytes.data(), size, CanCapability::CanFd, FrameDirection::Rx).error == CodecError::InvalidLength);
        CanStreamParser parser(CanCapability::CanFd);
        auto first = parser.feed(bytes.data(), size);
        CHECK(first); CHECK(first.value.empty()); CHECK(!parser.poisoned()); CHECK(parser.buffered_size() == size);
        auto second = parser.feed(bytes.data() + size, bytes.size() - size);
        CHECK(second); CHECK(second.value.size() == 1); CHECK(same_frame(second.value[0], goldens[2].frame)); CHECK(parser.buffered_size() == 0);
      }
    });
    test("every_frame_and_payload_split", [] {
      for (std::size_t dlc = 0; dlc < 16; ++dlc) {
        Bytes bytes(6 + kCanDlcLengths[dlc], 0); bytes[0] = static_cast<std::uint8_t>((dlc << 4U) | 1U); bytes[5] = bytes[0];
        for (std::size_t split = 0; split <= bytes.size(); ++split) {
          CanStreamParser parser(CanCapability::CanFd);
          auto first = parser.feed(bytes.data(), split);
          auto second = parser.feed(bytes.data() + split, bytes.size() - split);
          CHECK(first); CHECK(second); CHECK(first.value.size() + second.value.size() == 1); CHECK(parser.buffered_size() == 0);
          const auto &frame = first.value.empty() ? second.value[0] : first.value[0];
          CHECK(frame.fd); CHECK(frame.payload == Bytes(kCanDlcLengths[dlc], 0));
        }
      }
    });
    test("stream_64_byte_boundaries_and_multiple_frames", [] {
      Bytes stream; std::vector<CanFrame> expected;
      for (unsigned repetition = 0; repetition < 31; ++repetition) for (const auto &golden : goldens) {
        const auto bytes = unhex(golden.wire); stream.insert(stream.end(), bytes.begin(), bytes.end()); expected.push_back(golden.frame);
      }
      for (std::size_t chunk = 1; chunk <= 71; ++chunk) {
        CanStreamParser parser(CanCapability::CanFd); std::vector<CanFrame> received;
        for (std::size_t offset = 0; offset < stream.size(); offset += chunk) {
          auto result = parser.feed(stream.data() + offset, std::min(chunk, stream.size() - offset));
          CHECK(result); CHECK(parser.buffered_size() < kCanMaxFrameSize);
          received.insert(received.end(), result.value.begin(), result.value.end());
        }
        CHECK(parser.buffered_size() == 0); CHECK(received.size() == expected.size());
        for (std::size_t i = 0; i < received.size(); ++i) CHECK(same_frame(received[i], expected[i]));
      }
    });
    test("atomic_error_poison_and_explicit_memory_clear", [] {
      auto good = unhex(goldens[2].wire); auto bad = good; bad[6] ^= 1U;
      Bytes stream = good; stream.insert(stream.end(), bad.begin(), bad.end()); stream.insert(stream.end(), good.begin(), good.end());
      CanStreamParser parser(CanCapability::CanFd);
      auto failed = parser.feed(stream.data(), stream.size());
      CHECK(failed.error == CodecError::BadChecksum); CHECK(failed.value.empty()); CHECK(parser.poisoned()); CHECK(parser.buffered_size() == 0);
      CHECK(parser.feed(good.data(), good.size()).error == CodecError::Poisoned);
      CHECK(parser.feed(nullptr, 0).error == CodecError::Poisoned);
      parser.clear(); CHECK(!parser.poisoned());
      auto recovered = parser.feed(good.data(), good.size()); CHECK(recovered); CHECK(recovered.value.size() == 1);
      parser.clear(); CHECK(parser.feed(good.data(), 5)); CHECK(parser.buffered_size() == 5);
      parser.clear(); CHECK(parser.buffered_size() == 0);
      CHECK(parser.feed(good.data(), good.size()).value.size() == 1);
    });
    test("bounded_inputs_and_maximum_residue", [] {
      CanStreamParser parser(CanCapability::CanFd);
      Bytes largest(70, 0); largest[0] = 0xf1; largest[5] = 0xf1;
      CHECK(parser.feed(largest.data(), 69)); CHECK(parser.buffered_size() == 69);
      auto final = parser.feed(largest.data() + 69, 1); CHECK(final); CHECK(final.value.size() == 1);
      Bytes max_input(kCanMaxInputSize, 0);
      auto result = parser.feed(max_input.data(), max_input.size()); CHECK(result); CHECK(result.value.size() == kCanMaxInputSize / 6); CHECK(parser.buffered_size() == kCanMaxInputSize % 6);
      // An oversized declared length is rejected BEFORE touching the pointer.
      CHECK(parser.feed(largest.data(), kCanMaxInputSize + 1).error == CodecError::InputTooLarge); CHECK(parser.buffered_size() == 0);
      parser.clear(); CHECK(parser.feed(nullptr, 0));
      CHECK(parser.feed(nullptr, 1).error == CodecError::InvalidArgument); CHECK(parser.poisoned());
      CHECK(decode_can_frame(nullptr, 0, CanCapability::CanFd, FrameDirection::Rx).error == CodecError::InvalidLength);
      CHECK(decode_can_frame(nullptr, 6, CanCapability::CanFd, FrameDirection::Rx).error == CodecError::InvalidArgument);
    });
    test("independent_parser_state", [] {
      const auto bytes = unhex(goldens[2].wire);
      CanStreamParser first(CanCapability::CanFd), second(CanCapability::CanFd);
      CHECK(first.feed(bytes.data(), 5)); CHECK(second.feed(bytes.data(), 2));
      CHECK(first.buffered_size() == 5); CHECK(second.buffered_size() == 2);
      auto tail = Bytes(bytes.begin() + 5, bytes.end()); tail.back() ^= 1U;
      CHECK(!first.feed(tail.data(), tail.size())); CHECK(first.poisoned()); CHECK(!second.poisoned());
      auto success = second.feed(bytes.data() + 2, bytes.size() - 2); CHECK(success); CHECK(success.value.size() == 1);
    });
    test("host_routing_all_slots_flags_and_forbidden_sources", [] {
      for (unsigned slot = 0; slot < 2; ++slot) for (unsigned bus = 0; bus < 3; ++bus) {
        for (unsigned flag = 0; flag < 3; ++flag) {
          const auto source = host_source(slot, bus, flag == 1, flag == 2, FrameDirection::Rx); CHECK(source);
          CHECK(source.value == 4 * slot + bus + (flag == 1 ? 0x80U : (flag == 2 ? 0xc0U : 0U)));
          const auto route = parse_host_source(source.value, FrameDirection::Rx); CHECK(route);
          CHECK(route.value.slot == slot); CHECK(route.value.local_bus == bus); CHECK(route.value.returned == (flag == 1)); CHECK(route.value.rejected == (flag == 2));
          CHECK(static_cast<bool>(parse_host_source(source.value, FrameDirection::Tx)) == (flag == 0));
          CHECK(static_cast<bool>(host_source(slot, bus, flag == 1, flag == 2, FrameDirection::Tx)) == (flag == 0));
        }
        CHECK(host_source(slot, bus, true, true, FrameDirection::Rx).error == CodecError::InvalidFlags);
      }
      for (unsigned source = 0; source <= 512; ++source) {
        const bool ordinary = source == 0 || source == 1 || source == 2 || source == 4 || source == 5 || source == 6;
        const bool returned = source == 128 || source == 129 || source == 130 || source == 132 || source == 133 || source == 134;
        const bool rejected = source == 192 || source == 193 || source == 194 || source == 196 || source == 197 || source == 198;
        CHECK(static_cast<bool>(parse_host_source(source, FrameDirection::Rx)) == (ordinary || returned || rejected));
        CHECK(static_cast<bool>(parse_host_source(source, FrameDirection::Tx)) == ordinary);
      }
      CHECK(!parse_host_source(UINT32_MAX, FrameDirection::Rx));
      CHECK(!host_source(UINT32_MAX, 0, false, false, FrameDirection::Rx));
      CHECK(!host_source(0, UINT32_MAX, false, false, FrameDirection::Rx));
      CHECK(!host_source(2, 0, false, false, FrameDirection::Rx));
      CHECK(!host_source(0, 3, false, false, FrameDirection::Rx));
    });
    test("deterministic_malformed_fuzz_seed_c0ffee11", [] {
      std::uint32_t state = 0xc0ffee11U;
      const auto next = [&state]() { state ^= state << 13U; state ^= state >> 17U; state ^= state << 5U; return state; };
      for (unsigned iteration = 0; iteration < 4096; ++iteration) {
        Bytes bytes(next() % 256U); for (auto &b : bytes) b = static_cast<std::uint8_t>(next());
        const auto capability = (next() & 1U) ? CanCapability::CanFd : CanCapability::ClassicOnly;
        const auto direction = (next() & 1U) ? FrameDirection::Rx : FrameDirection::Tx;
        CanStreamParser parser(capability, direction);
        auto result = parser.feed(bytes.data(), bytes.size());
        CHECK(parser.buffered_size() < kCanMaxFrameSize);
        CHECK(parser.poisoned() == !static_cast<bool>(result));
        if (!result) CHECK(result.value.empty());
        else for (const auto &frame : result.value) {
          CHECK(frame.local_bus <= 2); CHECK(frame.payload.size() <= 64);
          CHECK(!(frame.returned && frame.rejected));
          CHECK(direction != FrameDirection::Tx || !(frame.returned || frame.rejected));
          CHECK(capability != CanCapability::ClassicOnly || (!frame.fd && frame.payload.size() <= 8));
        }
      }
    });
    test("invalid_policy_enum_rejected", [] {
      CanFrame frame;
      const auto bad_capability = static_cast<CanCapability>(999);
      const auto bad_direction = static_cast<FrameDirection>(999);
      CHECK(encode_can_frame(frame, bad_capability, FrameDirection::Rx).error == CodecError::InvalidArgument);
      CHECK(encode_can_frame(frame, CanCapability::CanFd, bad_direction).error == CodecError::InvalidArgument);
      const auto bytes = unhex(goldens[0].wire);
      CHECK(decode_can_frame(bytes.data(), bytes.size(), bad_capability, FrameDirection::Rx).error == CodecError::InvalidArgument);
      CanStreamParser parser(bad_capability); CHECK(parser.feed(nullptr, 0).error == CodecError::InvalidArgument);
      CHECK(host_source(0, 0, false, false, bad_direction).error == CodecError::InvalidArgument);
      CHECK(parse_host_source(0, bad_direction).error == CodecError::InvalidArgument);
    });
    std::cout << "{\"passed\":" << groups << ",\"failed\":0,\"assertions\":" << assertions
              << ",\"fuzz_seed\":\"c0ffee11\",\"fuzz_iterations\":4096,\"device_access\":false}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "FAIL after " << groups << " groups: " << error.what() << '\n';
    return 1;
  }
}
