# Settings Presets (SoW 205195 §1.8, requirements #23–#29)

This document describes the settings preset system and, importantly, **how to
swap in the real CS3.1 preset values** once they are available (they were
still "[I'll add this later]" in SoW Rev 06, so the current CS3.1 values are
dummies — see below).

## File layout

All files live in `[program data]/Settings/` (SoW #27) —
`%LOCALAPPDATA%\CopterSondeGCS\Settings` on Windows, the app's internal
storage directory on Android. Never on the microSD card (SoW #2).

| File               | Purpose |
|--------------------|---------|
| `<Name>.json`      | A named preset. Written **only** by "Save to Preset" / "Save As New" on the Settings → App Settings tab. |
| `CS3.1.json`       | The default preset (SoW #24). Seeded from in-code values on startup if missing or unreadable; never overwritten while valid. |
| `_autosave.json`   | Real-time placeholder (SoW #29). Rewritten atomically on **every** settings change, so a hard power-off of the Herelink loses nothing. The leading underscore keeps it out of the preset list. |
| `settings.json`    | Legacy pre-preset file. Read once as a migration source if no autosave exists, then ignored. Safe to delete. |

## Behavior

- **Startup (SoW #25/#26):** settings are resolved in this order:
  `_autosave.json` (last state, including unsaved edits) → legacy
  `settings.json` → `CS3.1.json` → in-code CS3.1 defaults. The user is not
  notified when defaults are substituted (per the SoW's scope shortcut).
- **Selecting a preset (SoW #23):** the "Preset File" spinner on the App
  Settings tab loads that file, replacing all settings-tab options and
  hot-reloading dependent state (wind coefficients, Remote ID identity,
  theme) exactly as at startup. Because a Kivy spinner does not fire an
  event when the already-selected entry is re-picked, the **Reload** button
  next to it covers #23's "selecting the same file overwrites temporary
  edits" case.
- **Saving:** "Save to Preset" overwrites the selected preset file with the
  current settings; "Save As New" creates a new file (name is filtered to
  filename-safe characters; leading `_` and `.` are stripped so user presets
  cannot collide with the autosave namespace).
- **Unsaved indicator (SoW #29):** a status line under the spinner shows
  either `Settings saved to preset '<name>'` or
  `Auto-recovered — changes not saved to '<name>'`. It compares the live
  settings against a snapshot of the selected preset file and refreshes
  continuously while the Settings screen is open.
- **Reset Defaults buttons removed (SoW #28):** from the Alert Thresholds,
  Wind Coefficients, and App Settings context — and also from the Remote ID
  tab, which the SoW predates but where the same redundancy rationale
  applies. Resetting is now done by reloading a preset (e.g. CS3.1).
- **Session state is not part of presets.** The Connection screen's last
  selection (`last_preset`, `last_conn_type`, `last_ip`, `last_port`) and
  the preset pointer itself ride along in the autosave but are excluded
  from preset files, so loading a preset never changes your connection
  setup.

## DUMMY CS3.1 VALUES — how to swap in the real ones

The real CS3.1 values from SoW #24 were never supplied. Until they are, the
CS3.1 preset simply reuses the application's pre-existing defaults, defined
in `app/main.py`:

- `DEFAULT_THRESHOLDS` — alert thresholds
- `DEFAULT_WIND_COEFFS` — wind coefficients
- `DEFAULT_STREAM_RATE_HZ`, `DEFAULT_REPLAY_OUTPUTS`, `ODID_DEFAULTS`
- assembled into a preset payload by `_cs31_settings()` (marked with a
  `!!! DUMMY VALUES !!!` comment)

To install the real values:

1. Edit the values in `app/main.py` — either directly in the `DEFAULT_*`
   dicts (if the real CS3.1 values should also be the in-code fallback,
   which SoW #26 implies) or by overriding individual keys inside
   `_cs31_settings()`.
2. Rebuild/redeploy.
3. On each deployed device, **delete
   `[program data]/Settings/CS3.1.json`** (or overwrite its contents with
   the new values by hand). The file is only seeded when missing or
   unreadable, precisely so that a user-saved CS3.1 is never clobbered —
   which means a code change alone does not update an existing file.

The in-code fallback (used when no settings file of any kind can be read,
SoW #26) always reflects the code, so step 3 only affects devices that have
already run an older build.

## Notes for maintainers

- `app.settings_data` is the same plain dict it always was, and
  `_save_settings(data)` kept its signature — every pre-existing call site
  works unchanged; only the destination moved from `settings.json` to
  `_autosave.json`.
- All settings writes go through `_atomic_write_json()` (temp file +
  `os.replace`), so a mid-write power loss cannot leave a truncated file.
- The saved/unsaved comparison uses a JSON-round-tripped, session-key-
  stripped payload (`_normalized_payload`) against a snapshot of the preset
  file (`app.preset_snapshot`), avoiding float/int and key-order artifacts.
