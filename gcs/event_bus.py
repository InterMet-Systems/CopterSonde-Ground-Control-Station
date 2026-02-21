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
    """Thread-safe event bus that dispatches on the Kivy main thread.

    Events are emitted from background threads (MAVLink IO, sim generator)
    but Kivy widgets can only be touched from the main thread.
    Clock.schedule_once bridges this gap safely.
    """

    def __init__(self):
        self._subscribers: dict[EventType, list] = {}
        self._lock = threading.Lock()  # protects _subscribers dict

    def subscribe(self, event_type: EventType, callback):
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def unsubscribe(self, event_type: EventType, callback):
        with self._lock:
            try:
                self._subscribers.get(event_type, []).remove(callback)
            except ValueError:
                pass  # silently ignore if callback was already removed

    def has_subscribers(self, event_type: EventType) -> bool:
        """Return True if any callbacks are registered for this event type.

        Used by the IO loop to skip snapshot() + emit overhead when no
        UI screen is listening (e.g. during settings or param editor).
        """
        with self._lock:
            return bool(self._subscribers.get(event_type))

    def emit(self, event_type: EventType, data=None):
        """Emit an event.  Callbacks run on the Kivy main thread."""
        # Snapshot the callback list under the lock so we don't hold the
        # lock while scheduling (which could deadlock if a callback
        # tries to subscribe/unsubscribe).
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))
        for cb in callbacks:
            # Clock.schedule_once defers execution to the next Kivy frame
            # on the main thread — required because Kivy widgets are not
            # thread-safe.  The default-arg trick (_cb=cb, _d=data) captures
            # the current loop variable values to avoid late-binding issues.
            Clock.schedule_once(lambda dt, _cb=cb, _d=data: _cb(_d), 0)
