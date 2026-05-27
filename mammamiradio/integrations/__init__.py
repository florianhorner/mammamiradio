"""Read-only integration contracts for external music controllers.

The v1 now-playing contract at ``/api/integrations/v1/now-playing`` is the
canonical entry point for Music Assistant, custom Home Assistant cards, and
future provider authors. The contract is documented at
``docs/integrations/now-playing.md`` and pinned by tests under
``tests/integrations/``.
"""

from mammamiradio.integrations.now_playing import router

__all__ = ["router"]
