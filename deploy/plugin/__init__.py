"""Plugin shim.

Hermes scans ``/opt/hermes/plugins/<name>/`` for a plugin module. The provider
itself lives in site-packages (``hermes_sandbox``), so this stays a two-line
re-export and the plugin directory carries only identity and metadata.
"""

from hermes_sandbox import register

__all__ = ["register"]
