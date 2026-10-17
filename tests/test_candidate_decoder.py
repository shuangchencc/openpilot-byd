import unittest

from decoding.decoder import ReferenceError, decode_frames
from replay.canlog import Frame, ValidationError


def decode(*frames):
  return decode_frames(tuple(frames), profile_id="official_atto3_reference", bus=0, data_kind="synthetic")


class CandidateDecoderTests(unittest.TestCase):
  def test_signed_angle_is_only_a_candidate(self):
    result = decode(Frame(1_000_000_000, 0, 0x11F, bytes.fromhex("85ff000001")))
    event = result["events"][0]
    self.assertAlmostEqual(event["signals"]["STEER_ANGLE_2"], -12.3)
    self.assertEqual(event["status"], "decoded_candidate")
    self.assertEqual(event["integrity_fields_without_validator"], ["CHECKSUM_4BIT"])
    self.assertEqual(result["vehicle_compatibility"], "unverified")
    self.assertFalse(result["actuation_allowed"])

  def test_short_frame_does_not_become_zero_filled_state(self):
    result = decode(Frame(1, 0, 0x11F, bytes.fromhex("7b00")))
    self.assertEqual(result["events"][0]["status"], "length_mismatch")
    self.assertIsNone(result["events"][0]["signals"])

  def test_bad_checksum_never_returns_stale_signals(self):
    result = decode(Frame(1_000_000_000, 0, 0x1F0, bytes.fromhex("6801000000001086")),
                    Frame(1_020_000_000, 0, 0x1F0, bytes.fromhex("6801000000002000")))
    first, second = result["events"]
    self.assertAlmostEqual(first["signals"]["WHEELSPEED_CLEAN"], 36)
    self.assertEqual(second["status"], "rejected_by_reference_parser")
    self.assertIsNone(second["signals"])

  def test_wrong_bus_and_unknown_address_are_not_decoded(self):
    result = decode(Frame(1, 4, 0x11F, bytes.fromhex("7b00000001")), Frame(2, 0, 0x7FF, b""))
    self.assertEqual([x["status"] for x in result["events"]], ["different_bus", "not_in_reference_dbc"])
    self.assertTrue(all(x["signals"] is None for x in result["events"]))

  def test_profile_cannot_be_selected_by_vehicle_name(self):
    with self.assertRaises(ReferenceError):
      decode_frames((), profile_id="BYD_SONG_PLUS_DMI_23_ALT", bus=0, data_kind="recorded")

  def test_python_api_rejects_invalid_frame_fields(self):
    for frame in (Frame(-1, 0, 0x11F, b"12345"), Frame(True, 0, 0x11F, b"12345"),
                  Frame(1, 256, 0x11F, b"12345"), Frame(1, 0, 0x20000000, b"12345"),
                  Frame(1, 0, 0x11F, b"123456789"), Frame(1, 0, 0x11F, "85ff000001")):
      with self.subTest(frame=frame), self.assertRaises(ValidationError):
        decode(frame)

  def test_python_api_rejects_backward_time_on_different_bus(self):
    with self.assertRaises(ValidationError):
      decode(Frame(2, 0, 0x11F, bytes.fromhex("85ff000001")), Frame(1, 4, 0x7FF, b""))

  def test_python_api_rejects_empty_recording(self):
    with self.assertRaises(ValidationError):
      decode()


if __name__ == "__main__":
  unittest.main()
