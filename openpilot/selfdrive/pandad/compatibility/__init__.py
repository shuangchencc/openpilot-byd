"""Offline-only dual-Panda contracts; importing this package opens no hardware.

This package is deliberately not imported by pandad. Use it as ``compatibility``
with this directory's parent on PYTHONPATH to avoid the existing pandad package
initializer and its cereal dependencies. No successful simulation authorizes
device access, real CAN transmission, or firmware installation.
"""
