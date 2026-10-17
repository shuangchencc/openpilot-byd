"""Offline dual-Panda contracts; this package opens no hardware.

The explicit pandad offline entry and opt-in consumer policy use these contracts.
Standalone tests import ``compatibility`` with its parent on PYTHONPATH to avoid
normal pandad package initialization. Successful simulation never authorizes
real device access, CAN transmission, or firmware installation.
"""
