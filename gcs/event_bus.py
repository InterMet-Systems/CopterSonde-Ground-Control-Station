"""
Kivy-adapted event bus for CopterSonde GCS.

Provides thread-safe publish/subscribe.  Callbacks registered from the
UI thread are dispatched on the main thread via ``Clock.schedule_once``
so subscribers can safely update Kivy widgets.
"""

import threading
from enum import Enum

from kivy.clock import Clock

from gcs.logutil import get_logger

log = get_logger("event_bus")


class EventType(Enum):
    DATA_UPDATED = "data_updated"
    CONNECTION_CHANGED = "connection_changed"
    ADSB_UPDATED = "adsb_updated"
    CLEAR_DATA = "clear_data"
    PARAM_RECEIVED = "param_received"


class EventBus:
    """Thread-safe event bus that dispatches on the Kivy main thread."""

    def __init__(self):
        self._subscribers: dict[EventType, list] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: EventType, callback):
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def unsubscribe(self, event_type: EventType, callback):
        with self._lock:
            try:
                self._subscribers.get(event_type, []).remove(callback)
            except ValueError:
                pass

    def has_subscribers(self, event_type: EventType) -> bool:
        """Return True if any callbacks are registered for this event type."""
        with self._lock:
            return bool(self._subscribers.get(event_type))

    def emit(self, event_type: EventType, data=None):
        """Emit an event.  Callbacks run on the Kivy main thread."""
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))
        for cb in callbacks:
            # Schedule on main thread so UI updates are safe
            Clock.schedule_once(lambda dt, _cb=cb, _d=data: _cb(_d), 0)
