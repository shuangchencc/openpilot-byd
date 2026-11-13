from opendbc.car.structs import CarParams
from opendbc.car.byd.values import CAR

Ecu = CarParams.Ecu

# Measured on bus 0 of a 2023 Song PLUS DM-i: the union of every address seen
# across 98 segments of a drive with the vendor stack running, 10 segments of a
# pure passive capture, and two power-up captures (2026-11-13). 98 addresses, no
# length conflict between any of them.
#
# A candidate is eliminated as soon as one observed address is missing from its
# fingerprint, so this wants to be a superset of everything the car can send. It
# is only the union of what has been seen, and it cannot be shown to be complete:
#
#   - For about 100 s after the car powers up, the car's own tester (gateway or
#     T-Box, 0x55 padding) polls a list of ECUs on this bus, one request a second:
#     10 01, 22 F1 94, 22 F1 95, 22 21 00, 19 02 09 to 0x701 0x704 0x760 0x761
#     0x770 0x771 0x7C2 0x7D4 0x7F2, with 0x704 answering from 0x70C. The
#     fingerprint window is about 2 s long and falls inside that scan whenever the
#     device powers up with the car, so without these the car was unrecognized on
#     both real boots that were measured (0x761 and 0x7C2 in one, 0x761 in the
#     other). car_helpers.can_fingerprint only ignores 0x7DF/0x7E0/0x7E8.
#   - 0x431 showed up as three frames 150 s into one drive and 0x43F in only some
#     recordings: event-driven messages exist, and the next one may not be here.
#
# So on the car the launcher also pins FINGERPRINT to this platform. Evidence:
# migration-audit/can-fingerprint-powerup-20261113/RESULT.txt.
FINGERPRINTS = {
  CAR.BYD_SONG_PLUS_DMI_23: [{
    140: 8, 213: 8, 269: 8, 287: 5, 289: 8, 291: 8,
    296: 8, 300: 8, 301: 8, 307: 8, 312: 8, 337: 8,
    482: 8, 493: 8, 496: 8, 508: 8, 536: 8, 544: 8,
    546: 8, 547: 8, 575: 8, 576: 8, 578: 8, 588: 8,
    604: 8, 660: 8, 661: 8, 663: 8, 692: 8, 694: 8,
    724: 8, 748: 8, 790: 8, 797: 8, 798: 8, 801: 8,
    802: 8, 803: 8, 813: 8, 814: 8, 815: 8, 833: 8,
    834: 8, 836: 8, 837: 8, 854: 8, 860: 8, 884: 8,
    904: 8, 906: 8, 944: 8, 951: 8, 965: 8, 973: 8,
    985: 8, 1023: 8, 1028: 8, 1031: 8, 1033: 8, 1036: 8,
    1040: 8, 1050: 8, 1052: 8, 1058: 8, 1074: 8, 1076: 8,
    1087: 8, 1092: 8, 1093: 8, 1097: 8, 1098: 8, 1141: 8,
    1166: 8, 1168: 8, 1178: 8, 1189: 8, 1190: 8, 1192: 8,
    1193: 8, 1203: 64, 1204: 64, 1214: 8, 1217: 8, 1223: 64,
    1226: 16, 1246: 8, 1297: 8,
    # event-driven, seen once (0x431)
    1073: 8,
    # the car's own power-up diagnostic scan: requests, then the one response on this bus
    1793: 8, 1796: 8, 1888: 8, 1889: 8, 1904: 8, 1905: 8,
    1986: 8, 2004: 8, 2034: 8,
    1804: 8,
  }],
}

FW_VERSIONS = {}
