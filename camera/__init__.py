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

"""Camera backends: the only place a vendor's protocol lives.

See base.CameraBackend for the contract, and registry for how a config picks
one. Adding a camera means writing a subclass -- not editing the core.
"""

from .base import (Cap, CameraBackend, CameraError, NotSupported,
                   UrllibTransport)
from .foscam import FoscamBackend
from .registry import available, backend_class, make_backend, register

__all__ = ["Cap", "CameraBackend", "CameraError", "NotSupported",
           "UrllibTransport", "FoscamBackend",
           "available", "backend_class", "make_backend", "register"]
