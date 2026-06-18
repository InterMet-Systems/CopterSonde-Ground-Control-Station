"""
Robust writable-storage resolution for CopterSonde GCS.

Android storage is version- and OEM-dependent and the GCS runs on a
console-less HereLink, so this module tries several candidate base
directories (most reliable first), test-writes to each, uses the first
that works, and records every attempt so the chosen path — or any
failure — can be displayed on-device.
"""

import os

from gcs.logutil import get_logger

log = get_logger("storage")

try:
    import android  # noqa: F401
    ON_ANDROID = True
except ImportError:
    ON_ANDROID = False

_attempts = []        # [(label, path, ok, detail)] from the last resolve
_resolved = {}        # subdir -> resolved absolute path (cache)


def _candidate_bases(prefer_removable=False):
    """Ordered (label, base_dir) candidates, most reliable first.

    When ``prefer_removable`` is True, the removable SD-card app dir(s) are
    tried ahead of built-in storage.  Used for the telemetry log, which must
    live on the micro SD card on Android (SoW #31).  If no card is present
    the removable list is empty and resolution falls through to built-in
    storage so logs are never lost.
    """
    if not ON_ANDROID:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return [("desktop", repo_root)]

    # 1) App-specific external dirs via the Android API. No permission on
    #    ANY Android version. Index 0 = built-in shared storage's app area;
    #    index 1+ = the removable SD card's app area (only if a card is in).
    #    Paths look like /storage/emulated/0/Android/data/<pkg>/files for
    #    index 0, /storage/<UUID>/Android/data/<pkg>/files for the card.
    builtin = []     # index 0 — built-in (emulated) storage
    removable = []   # index 1+ — removable SD card(s)
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        for i, f in enumerate(activity.getExternalFilesDirs(None)):
            if f is None:
                continue
            if i == 0:
                builtin.append(("app-external", f.getAbsolutePath()))
            else:
                removable.append((f"app-external-sd{i}", f.getAbsolutePath()))
    except Exception as exc:
        _attempts.append(("app-external", "(jnius)", False, repr(exc)))

    # SD card first when the caller asked for it; otherwise built-in first
    # (preserves the previous default ordering for all other consumers).
    cands = removable + builtin if prefer_removable else builtin + removable

    # 2) Primary shared storage — user-browsable, but needs
    #    WRITE_EXTERNAL_STORAGE and is blocked on Android 11+/targetSdk 30+.
    try:
        from android.storage import primary_external_storage_path
        cands.append(("primary-external",
                      os.path.join(primary_external_storage_path(),
                                   "CopterSondeGCS")))
    except Exception:
        pass

    # 3) App-private internal — always writable, not browsable without root.
    #    Last resort so data is never silently lost.
    try:
        from android.storage import app_storage_path
        cands.append(("app-private", app_storage_path()))
    except Exception:
        pass

    return cands


def resolve_base(subdir="", prefer_removable=False):
    """Return a writable directory (with optional subdir), first that works.

    Set ``prefer_removable`` to prefer the micro SD card over built-in
    storage on Android (used for the telemetry log, SoW #31).
    """
    global _attempts
    cache_key = (subdir, prefer_removable)
    if cache_key in _resolved:
        return _resolved[cache_key]

    _attempts = []
    for label, base in _candidate_bases(prefer_removable):
        path = os.path.join(base, subdir) if subdir else base
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            _attempts.append((label, path, True, "writable"))
            _resolved[cache_key] = path
            log.info("Storage[%s]: using %s (%s)",
                     subdir or "base", path, label)
            return path
        except Exception as exc:
            _attempts.append((label, path, False, repr(exc)))
            log.warning("Storage[%s]: %s not writable (%s): %s",
                        subdir or "base", path, label, exc)

    fallback = os.path.abspath(subdir or ".")
    _attempts.append(("cwd-fallback", fallback, True, "last resort"))
    _resolved[cache_key] = fallback
    return fallback


def report():
    """Human-readable summary of the last resolution, for on-device display."""
    lines = ["Storage resolution (most recent):"]
    for label, path, ok, detail in _attempts:
        lines.append(f"  {'OK' if ok else 'xx'} [{label}] {path}  ({detail})")
    if not _attempts:
        lines.append("  (not resolved yet)")
    return "\n".join(lines)