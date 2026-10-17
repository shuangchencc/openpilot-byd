"""Pass the pinned, project-local LLVM library to official SCons child processes.

The official SConstruct deliberately creates a small subprocess environment.
This host-only site hook preserves LLVM_PATH without changing upstream sources.
It does not replace compiler flags, model settings, or any build target.
"""
import os

from SCons.Environment import Base


_original_init = Base.__init__


def _init_with_host_llvm(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    if llvm_path := os.environ.get("LLVM_PATH"):
        self["ENV"]["LLVM_PATH"] = llvm_path


Base.__init__ = _init_with_host_llvm
