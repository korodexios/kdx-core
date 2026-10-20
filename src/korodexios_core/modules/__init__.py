"""Built-in module packages. This directory is the chassis discovery root.

Discovery imports every subpackage (with __init__.py) and every single-file
module found here, validates them against the module contract, and registers the
survivors. Core infrastructure never lives here; it lives in 'kernel/'.
"""

from __future__ import annotations

__all__: list[str] = []
