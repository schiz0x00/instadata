"""Browser tier. Imported lazily; Chromium never launches unless escalated to."""

from .transport import BrowserTransport

__all__ = ["BrowserTransport"]
