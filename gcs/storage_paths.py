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


def _candidate_bases():
    """Ordered (label, base_dir) candidates, most reliable first."""
    if not ON_ANDROID:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return [("desktop", repo_root)]

    cands = []
    # 1) App-specific external dirs via the Android API. No permission on
    #    ANY Android version. Index 0 = built-in shared storage's app area;
    #    index 1+ = the removable SD card's app area (only if a card is in).
    #    Path looks like /storage/emulated/0/Android/data/<pkg>/files.
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        for i, f in enumerate(activity.getExternalFilesDirs(None)):
            if f is None:
                continue
            label = "app-external" if i == 0 else f"app-external-sd{i}"
            cands.append((label, f.getAbsolutePath()))
    except Exception as exc:
        _attempts.append(("app-external", "(jnius)", False, repr(exc)))

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


def resolve_base(subdir=""):
    """Return a writable directory (with optional subdir), first that works."""
    global _attempts
    if subdir in _resolved:
        return _resolved[subdir]

    _attempts = []
    for label, base in _candidate_bases():
        path = os.path.join(base, subdir) if subdir else base
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            _attempts.append((label, path, True, "writable"))
            _resolved[subdir] = path
            log.info("Storage[%s]: using %s (%s)",
                     subdir or "base", path, label)
            return path
        except Exception as exc:
            _attempts.append((label, path, False, repr(exc)))
            log.warning("Storage[%s]: %s not writable (%s): %s",
                        subdir or "base", path, label, exc)

    fallback = os.path.abspath(subdir or ".")
    _attempts.append(("cwd-fallback", fallback, True, "last resort"))
    _resolved[subdir] = fallback
    return fallback


def report():
    """Human-readable summary of the last resolution, for on-device display."""
    lines = ["Storage resolution (most recent):"]
    for label, path, ok, detail in _attempts:
        lines.append(f"  {'OK' if ok else 'xx'} [{label}] {path}  ({detail})")
    if not _attempts:
        lines.append("  (not resolved yet)")
    return "\n".join(lines)