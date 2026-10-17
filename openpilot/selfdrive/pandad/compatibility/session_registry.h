#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace openpilot::pandad::compatibility {

inline constexpr const char *kSessionProfileSchema = "songplus-dual-panda-offline-profile/v1";
inline constexpr const char *kSessionProtocol = "current:f512bcff76f2ab75";
inline constexpr std::int32_t kRoleUnassigned = -1;
inline constexpr std::int32_t kRoleUnsupported = -2;

struct SessionCapabilities {
  std::array<std::uint32_t, 3> can_buses{{0, 1, 2}};
  bool can_fd = false;
  bool diagnostic = false;
  bool harness = false;
  bool ignition = false;
  bool fan = false;
  bool power_monitor = false;
  bool ir = false;
};
bool operator==(const SessionCapabilities &a, const SessionCapabilities &b);

struct SessionIdentity {
  std::string identity;
  std::string board_type;
  std::string mcu_family;
  std::string exact_mcu;
  std::string transport;
  std::string protocol_profile;
  SessionCapabilities capabilities{};
  std::string application_signature;
};
bool operator==(const SessionIdentity &a, const SessionIdentity &b);

struct SessionBinding {
  std::uint32_t slot = 0;
  SessionIdentity expected{};
  std::string safety_config_semantics;
};

struct SessionRoles {
  std::int32_t vehicle_can = kRoleUnassigned;
  std::int32_t diagnostic = kRoleUnassigned;
  std::int32_t harness = kRoleUnassigned;
  std::vector<std::uint32_t> ignition_sources{};
  std::int32_t fan_control = kRoleUnassigned;
  std::int32_t power_monitor = kRoleUnassigned;
  std::int32_t ir_control = kRoleUnassigned;
};

// Typed equivalent of P0's strict profile. The constructor validates every
// field and stores its own immutable copy; mutating this input afterwards has
// no effect. Empty bindings/roles describe UNBOUND and can never activate.
struct SessionProfile {
  std::string schema = kSessionProfileSchema;
  std::string purpose = "offline_fixture";
  std::string binding_status = "UNBOUND";
  bool device_access_enabled = false;
  bool can_tx_enabled = false;
  bool firmware_install_enabled = false;
  std::uint32_t expected_board_count = 2;
  std::array<std::uint32_t, 2> required_slots{{0, 1}};
  std::uint32_t bus_stride = 4;
  std::array<std::uint32_t, 3> ordinary_local_can_buses{{0, 1, 2}};
  std::array<SessionBinding, 2> slots{{SessionBinding{0, {}, {}}, SessionBinding{1, {}, {}}}};
  SessionRoles roles{};
};

struct SessionObservation {
  SessionIdentity identity{};
  // Supplied by the offline discovery adapter, not a USB address or serial.
  // Strictly increasing per bound identity for each new handle. Zero is invalid.
  // Repeating the current live token is idempotent; a retired token or a token
  // consumed during descriptor validation/duplicate quarantine cannot reconnect.
  // Oversized inventories are rejected before inspecting individual candidates;
  // those candidates are not registered. UINT64_MAX cannot wrap back to zero.
  std::uint64_t connection_token = 0;
};

struct SlotSnapshot {
  std::uint32_t slot = 0;
  SessionBinding binding{};
  bool connected = false;
  std::uint64_t epoch = 0;
  std::uint64_t connection_token = 0;
};

struct SessionSnapshot {
  std::array<SlotSnapshot, 2> slots{};
  std::uint64_t generation = 0;
  // Structural identity/inventory availability only. P4 health/safety and live
  // vehicle permission are deliberately absent and always remain denied.
  bool group_available = false;
  bool permanently_closed = false;
  std::string reason = "UNBOUND_PROFILE";
};

// Only seeds an EMPTY offline registry for deterministic overflow fixtures.
// Cannot restore connections, create a live snapshot, or grant permission.
struct SessionCounterSeed {
  std::array<std::uint64_t, 2> epochs{{0, 0}};
  std::uint64_t generation = 0;
};

class SessionRegistry {
public:
  explicit SessionRegistry(const SessionProfile &profile, SessionCounterSeed seed = {});
  SessionRegistry(const SessionRegistry &) = delete;
  SessionRegistry &operator=(const SessionRegistry &) = delete;

  // Single serialized event-loop ownership is required. There are no worker
  // threads, locks, discovery calls, I/O, or callbacks. Reconcile the COMPLETE
  // inventory: no incremental discovery or implicit surviving-board promotion.
  SessionSnapshot reconcile(const std::vector<SessionObservation> &inventory);
  // Stale completion/disconnect events do not invalidate a newer connection.
  bool disconnect(std::uint32_t slot, std::uint64_t epoch, std::uint64_t connection_token);
  // A malformed stream/overflow/clock failure retires all live tokens. Healing
  // requires fresh handles and a complete validated inventory, never old tokens.
  void invalidate_group(const std::string &reason);

  SessionSnapshot snapshot() const { return state_; }
  SessionProfile profile() const { return profile_; }
  bool device_access_enabled() const { return false; }
  bool can_tx_enabled() const { return false; }
  bool firmware_install_enabled() const { return false; }

private:
  bool advance(std::uint64_t &counter);
  void exhaust();
  bool close_slot(std::size_t slot);
  void finalize(bool changed, bool available, const std::string &reason);
  const SessionProfile profile_;
  SessionSnapshot state_{};
  std::array<std::uint64_t, 2> token_high_watermark_{{0, 0}};
};

}  // namespace openpilot::pandad::compatibility
