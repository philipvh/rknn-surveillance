# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. This
# software aids surveillance; it does not guarantee it, and no liability is
# accepted for any failure to detect, record, retain or report an event.
# See the NOTICE file for the full disclaimer.

"""Which backend a config asks for.

    camera:
      type: foscam                      # a name registered here
      type: mypkg.driver:AxisBackend    # or any importable class

The dotted form matters: someone with an unusual camera can keep their driver
in their own package and never fork this repo. That is the difference between
an extension point and an invitation to maintain a patch.
"""

from __future__ import annotations

import importlib
import logging

from .base import CameraBackend, CameraError
from .foscam import FoscamBackend

log = logging.getLogger("camera.registry")

_BACKENDS = {}


def register(cls, name=None):
    """Make a backend available under `camera.type`."""
    key = (name or getattr(cls, "name", "")).strip().lower()
    if not key:
        raise ValueError("a backend needs a name")
    if not issubclass(cls, CameraBackend):
        raise TypeError(f"{cls!r} is not a CameraBackend")
    _BACKENDS[key] = cls
    return cls


register(FoscamBackend)


def available():
    return sorted(_BACKENDS)


def backend_class(spec):
    """Resolve a `camera.type` to a class, by name or dotted path."""
    spec = (spec or "foscam").strip()
    key = spec.lower()
    if key in _BACKENDS:
        return _BACKENDS[key]
    if ":" in spec:                       # module.path:ClassName
        mod_name, _, cls_name = spec.partition(":")
        try:
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, cls_name)
        except (ImportError, AttributeError) as e:
            raise CameraError(
                f"cannot load camera backend {spec!r}: {e}") from e
        if not (isinstance(cls, type) and issubclass(cls, CameraBackend)):
            raise CameraError(f"{spec} is not a CameraBackend")
        return cls
    raise CameraError(
        f"unknown camera type {spec!r}. Known: {', '.join(available())}. "
        f"For a driver of your own use 'module.path:ClassName'.")


def make_backend(cfg, transport=None, timeout=5.0):
    spec = cfg._get("camera", "type", default="foscam")
    cls = backend_class(spec)
    return cls(cfg, transport=transport, timeout=timeout)
