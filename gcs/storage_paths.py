"""
Robust writable-storage resolution for CopterSonde GCS.

Android storage is version- and OEM-dependent and the GCS runs on a
console-less HereLink, so this module tries several candidate base
directories (most reliable first), test-writes to each, uses the first
that works, and records every attempt so the chosen path — or any
failure — can be displayed on-device.

This module also resolves the two directory "macros" defined in SoW
205195 §1.2 and implements its mirroring policy:

  #1 [usr access intended] — ``user_locations()`` / ``user_dir()``.
     Windows: Documents/CopterSondeGCS (override: CGCS_USER_DATA_DIR).
     Android: the SD card's app-specific dir when a card is mounted,
     otherwise built-in storage's app-specific dir.
  #2 [program data] — ``program_data_dir()``.  Windows: %LOCALAPPDATA%.
     Android: built-in storage's app-specific dir; never the SD card.
  #3 SD→built-in mirroring — ``TeeFile`` / ``tee_open()`` for append-style
     writers, ``mirror_file()`` for one-shot writers.
  #4 A missing SD card is silent: it simply produces no candidate, so the
     primary becomes built-in storage and no backup is used.

``output_dirs()`` resolves the base once and joins subpaths onto it, so
all output trees (TelemetryLog, Messages/…) share one parent — writers
should use it rather than resolving subdirs independently.
"""

import os
import shutil
import sys

from gcs.logutil import get_logger

log = get_logger("storage")

try:
    import android  # noqa: F401
    ON_ANDROID = True
except ImportError:
    ON_ANDROID = False

_attempts = []        # [(label, path, ok, detail)] from resolution probes


def _android_external_dirs():
    """Enumerate app-specific external dirs: (builtin, removable, error).

    Index 0 of ``getExternalFilesDirs()`` is built-in (emulated) storage's
    app area (/storage/emulated/0/Android/data/<pkg>/files); indices 1+ are
    removable SD card app areas (/storage/<UUID>/Android/data/<pkg>/files),
    present only while a card is mounted.  No permission is needed on any
    Android version.  ``error`` is a repr string when enumeration itself
    failed (e.g. the activity isn't up yet), else None.
    """
    builtin, removable = [], []
    err = None
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        for i, f in enumerate(activity.getExternalFilesDirs(None)):
            if f is None:
                continue
            (builtin if i == 0 else removable).append(f.getAbsolutePath())
    except Exception as exc:
        err = repr(exc)
    return builtin, removable, err


def report():
    """Human-readable summary of resolution attempts, for on-device display."""
    lines = ["Storage resolution:"]
    for label, path, ok, detail in _attempts:
        lines.append(f"  {'OK' if ok else 'xx'} [{label}] {path}  ({detail})")
    if not _attempts:
        lines.append("  (not resolved yet)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SoW 205195 §1.2 macros and mirroring policy (#1–#4)
# ---------------------------------------------------------------------------

_USER_DIR_ENV = "CGCS_USER_DATA_DIR"   # desktop override for [usr access intended]
_APP_DIR_NAME = "CopterSondeGCS"

_user_cache = {}       # subdir -> (primary, backup_or_None)
_progdata_cache = {}   # subdir -> path


def _split_subdir(subdir):
    """Split 'Messages/Debug' into path components (accepts / or \\)."""
    return [p for p in subdir.replace("\\", "/").split("/") if p]


def _probe(path, label):
    """Create ``path`` and write-test it.  True if writable.

    Every probe is recorded in ``_attempts`` so report() can show the
    resolution history on-device.
    """
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        _attempts.append((label, path, True, "writable"))
        return True
    except Exception as exc:
        _attempts.append((label, path, False, repr(exc)))
        log.warning("Storage: %s not writable (%s): %s", path, label, exc)
        return False


def _windows_documents():
    """The user's Documents folder (follows OneDrive/policy redirection)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_PERSONAL (5) = Documents; SHGFP_TYPE_CURRENT (0)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            return buf.value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def _desktop_user_base():
    override = os.environ.get(_USER_DIR_ENV)
    if override:
        return override
    docs = (_windows_documents() if sys.platform == "win32"
            else os.path.join(os.path.expanduser("~"), "Documents"))
    return os.path.join(docs, _APP_DIR_NAME)


def user_locations(subdir=""):
    """Resolve ``[usr access intended]/subdir`` → (primary, backup) (SoW #1, #3).

    ``primary`` is where output must be written; ``backup`` is the built-in
    storage mirror to also write to when the primary is the removable SD
    card, else None.  Returned directories exist and are write-tested.

    A missing SD card is not an error (#4): it produces no removable
    candidate, so the primary silently becomes built-in storage and no
    backup is used (writing twice to the same volume would be pointless).
    """
    if subdir in _user_cache:
        return _user_cache[subdir]

    parts = _split_subdir(subdir)
    cacheable = True

    if not ON_ANDROID:
        primary = None
        for label, base in (("user-base", _desktop_user_base()),
                            ("home", os.path.join(os.path.expanduser("~"),
                                                  _APP_DIR_NAME))):
            path = os.path.join(base, *parts)
            if _probe(path, label):
                primary = path
                break
        if primary is None:                       # last resort, untested
            primary = os.path.join(os.getcwd(), *parts)
            cacheable = False
        result = (primary, None)
        if cacheable:
            _user_cache[subdir] = result
        return result

    builtin_paths, removable_paths, err = _android_external_dirs()
    if err:
        # The activity may not be up yet — don't cache, so a later call
        # can retry once enumeration works.
        cacheable = False
        _attempts.append(("app-external", "(jnius)", False, err))
        log.warning("Storage: getExternalFilesDirs failed: %s", err)

    primary = None
    for base in removable_paths:                  # SD card first (SoW #31)
        path = os.path.join(base, *parts)
        if _probe(path, "sd-card"):
            primary = path
            break
    on_card = primary is not None

    builtin_path = None
    for base in builtin_paths:
        path = os.path.join(base, *parts)
        if _probe(path, "built-in"):
            builtin_path = path
            break

    backup = None
    if on_card:
        backup = builtin_path                     # mirror target (SoW #3)
    elif builtin_path is not None:
        primary = builtin_path                    # no card: built-in is primary

    if primary is None:
        # Legacy fallbacks: user-visible shared storage, then app-private
        # internal so data is never lost.
        legacy = []
        try:
            from android.storage import primary_external_storage_path
            legacy.append(("primary-external",
                           os.path.join(primary_external_storage_path(),
                                        _APP_DIR_NAME)))
        except Exception:
            pass
        try:
            from android.storage import app_storage_path
            legacy.append(("app-private", app_storage_path()))
        except Exception:
            pass
        for label, base in legacy:
            path = os.path.join(base, *parts)
            if _probe(path, label):
                primary = path
                break
    if primary is None:                           # last resort, untested
        primary = os.path.abspath(os.path.join(*parts) if parts else ".")
        cacheable = False

    result = (primary, backup)
    if cacheable:
        _user_cache[subdir] = result
    return result


def user_dir(subdir=""):
    """Primary ``[usr access intended]`` directory (see user_locations)."""
    return user_locations(subdir)[0]


def output_dirs(*parts):
    """(primary, backup) leaf directories under one shared base resolution.

    Resolves the ``[usr access intended]`` *base* once (cached) and joins
    ``parts`` onto it, so every output tree — TelemetryLog, Messages/Raw,
    Messages/Debug, … — is guaranteed to share the same parent on every
    platform (SoW #11), which per-subdir resolution could not guarantee if
    volume availability changed between calls.  Leaf directories are NOT
    created here; writers create them at open time, as they always have.
    ``backup`` is None when there is nothing to mirror to (SoW #3/#4).
    """
    base, backup_base = user_locations("")
    primary = os.path.join(base, *parts)
    backup = os.path.join(backup_base, *parts) if backup_base else None
    return primary, backup


def program_data_dir(subdir=""):
    """Resolve ``[program data]/subdir`` (SoW #2): a directory the user is
    not expected to visit but that is not protected from access.  Never on
    the SD card.  Created and write-tested on return.
    """
    if subdir in _progdata_cache:
        return _progdata_cache[subdir]

    parts = _split_subdir(subdir)
    cacheable = True

    if not ON_ANDROID:
        bases = []
        if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
            bases.append(("localappdata",
                          os.path.join(os.environ["LOCALAPPDATA"],
                                       _APP_DIR_NAME)))
        bases.append(("home-dotdir",
                      os.path.join(os.path.expanduser("~"),
                                   ".coptersonde_gcs")))
    else:
        builtin_paths, _removable, err = _android_external_dirs()
        if err:
            cacheable = False
            _attempts.append(("app-external", "(jnius)", False, err))
            log.warning("Storage: getExternalFilesDirs failed: %s", err)
        bases = [("built-in", p) for p in builtin_paths]  # never the SD card (#2)
        try:
            from android.storage import app_storage_path
            # Root-protected, so it violates #2's "not protected from
            # access" — last resort only, and called out in the log.
            bases.append(("app-private", app_storage_path()))
        except Exception:
            pass

    path = None
    for label, base in bases:
        cand = os.path.join(base, *parts)
        if _probe(cand, label):
            path = cand
            if label == "app-private":
                log.warning("Program data on app-private storage "
                            "(not user-accessible): %s", cand)
            break
    if path is None:                              # last resort, untested
        path = os.path.abspath(os.path.join(*parts) if parts else ".")
        cacheable = False

    if cacheable:
        _progdata_cache[subdir] = path
    return path


# ---------------------------------------------------------------------------
# SD → built-in mirroring (SoW #3)
# ---------------------------------------------------------------------------

class TeeFile:
    """File-like object that duplicates writes to a backup handle.

    Primary-side errors propagate to the caller (all writers already catch
    them); a backup-side error permanently drops the backup for this file
    after a single warning, so a flaky volume can neither spam the log at
    telemetry rates nor stall primary writes.
    """

    def __init__(self, primary, backup=None):
        self._primary = primary
        self._backup = backup
        self.name = primary.name

    def write(self, data):
        n = self._primary.write(data)
        if self._backup is not None:
            try:
                self._backup.write(data)
            except Exception as exc:
                self._drop_backup("write", exc)
        return n

    def flush(self):
        self._primary.flush()
        if self._backup is not None:
            try:
                self._backup.flush()
            except Exception as exc:
                self._drop_backup("flush", exc)

    def close(self):
        try:
            self._primary.close()
        finally:
            if self._backup is not None:
                backup, self._backup = self._backup, None
                try:
                    backup.close()
                except Exception as exc:
                    log.warning("Backup close failed for %s: %s",
                                getattr(backup, "name", "?"), exc)

    @property
    def closed(self):
        return self._primary.closed

    def _drop_backup(self, op, exc):
        backup, self._backup = self._backup, None
        log.warning("Backup mirror dropped (failed %s) for %s: %s",
                    op, getattr(backup, "name", "?"), exc)
        try:
            backup.close()
        except Exception:
            pass


def tee_open(primary_dir, backup_dir, filename, mode="w", **open_kwargs):
    """Open ``filename`` in ``primary_dir``, mirrored in ``backup_dir``.

    Returns a TeeFile.  ``backup_dir`` may be None — the normal case on
    desktop or when no SD card is present — in which case this behaves as
    a plain open().  A failure to open the backup is non-fatal and logged;
    a failure to open the primary propagates, matching plain open().
    """
    primary = open(os.path.join(primary_dir, filename), mode, **open_kwargs)
    backup = None
    if backup_dir:
        try:
            os.makedirs(backup_dir, exist_ok=True)
            backup = open(os.path.join(backup_dir, filename), mode,
                          **open_kwargs)
        except Exception as exc:
            log.warning("Backup open failed in %s for %s: %s",
                        backup_dir, filename, exc)
    return TeeFile(primary, backup)


def mirror_file(src_path, backup_dir):
    """Copy a finished file into ``backup_dir`` (#3, for one-shot writers
    like the WMO netCDF).  No-op when ``backup_dir`` is None; failures are
    logged, never raised.
    """
    if not backup_dir:
        return
    try:
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(src_path, backup_dir)
    except Exception as exc:
        log.warning("Backup mirror of %s failed: %s", src_path, exc)