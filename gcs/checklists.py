"""
Checklist file loading (SoW 205195 #15-#17).

Checklists live as individual JSON files in ``[program data]/Checklists/``
(#17), discovered by the double extension ``.checklist.json`` — the
dedicated directory does most of the filtering, and the extension keeps
strays (backups, editor droppings, a misplaced settings.json) out of the
listing.  The schema is deliberately dirt simple:

    {
      "name": "Pre-Flight",
      "items": ["First check", "Second check", ...]
    }

``name`` is the display name shown in Settings and the popup title; it
falls back to the filename stem when absent.  ``items`` is a flat list of
strings.  Malformed or unreadable files are skipped from the listing with
a logged warning, never crash the UI, and an empty directory is legal —
the SoW allows a blank checklist window.

Whenever the Checklists directory is missing at startup — first run, or
someone deleted the folder — it is created and the previously hardcoded
pre-flight checklist is written into it, so the folder always carries a
working example of the format (pseudo-documentation for people who don't
read documentation).  A directory that exists but is empty is respected:
deleting every checklist file is treated as the operator's choice.
"""

import json
import os

from gcs.logutil import get_logger
from gcs.storage_paths import program_data_dir

log = get_logger("checklists")

EXTENSION = ".checklist.json"
DEFAULT_FILENAME = "preflight" + EXTENSION

# The checklist that shipped hardcoded before #15; now the first-run seed.
DEFAULT_CHECKLIST = {
    "name": "Pre-Flight",
    "items": [
        "Good weather and air traffic",
        "Battery installation",
        "Confirm good health status of the CopterSonde",
        "KP solar storm index lower than 5",
        "CopterSonde is place on the launch pad",
        "Mission is generated",
        "Approval from crew for flights",
    ],
}


def checklists_dir():
    """The ``[program data]/Checklists`` directory (created on resolve)."""
    return program_data_dir("Checklists")


def seed_default():
    """Create the Checklists folder + default file whenever the folder is
    missing at startup (first run or a deleted folder), checked before
    program_data_dir creates it.  A folder that exists but has been
    emptied of files stays empty across restarts.
    """
    base = program_data_dir("")
    first_run = not os.path.isdir(os.path.join(base, "Checklists"))
    d = checklists_dir()
    if not first_run:
        return
    path = os.path.join(d, DEFAULT_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CHECKLIST, fh, indent=2)
        log.info("Seeded default checklist: %s", path)
    except Exception:
        log.exception("Failed to seed default checklist")


def list_checklists():
    """Discover checklists: [(filename, display_name)], sorted by name.

    Malformed files are skipped (with a warning in the debug log) so one
    bad file can't hide the rest.
    """
    d = checklists_dir()
    found = []
    try:
        names = os.listdir(d)
    except OSError as exc:
        log.warning("Cannot list checklists in %s: %s", d, exc)
        return found
    for fn in names:
        if not fn.endswith(EXTENSION):
            continue
        data = _read(os.path.join(d, fn))
        if data is not None:
            found.append((fn, data["name"]))
    found.sort(key=lambda t: t[1].lower())
    return found


def load_checklist(filename):
    """Load one checklist by bare filename -> (name, items), or None.

    ``filename`` comes from settings; path components are rejected so a
    hand-edited settings.json can't read outside the Checklists folder.
    """
    if (not filename or os.sep in filename or "/" in filename
            or "\\" in filename):
        return None
    data = _read(os.path.join(checklists_dir(), filename))
    if data is None:
        return None
    return data["name"], data["items"]


def _read(path):
    """Parse + validate one checklist file -> {'name', 'items'} or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        log.warning("Skipping unreadable checklist %s: %s", path, exc)
        return None
    items = raw.get("items") if isinstance(raw, dict) else None
    if (not isinstance(items, list)
            or not all(isinstance(i, str) for i in items)):
        log.warning('Skipping malformed checklist %s: expected '
                    '{"name": str, "items": [str, ...]}', path)
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        name = os.path.basename(path)[:-len(EXTENSION)]
    return {"name": name.strip(), "items": items}
