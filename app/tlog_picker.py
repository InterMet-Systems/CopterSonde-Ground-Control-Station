"""
Telemetry-log file picker popup for CopterSonde GCS.

Implements interface contract item 3: ``open_tlog_picker(on_selected)``
opens a popup listing the recorded ``*.tlog`` files found in
``gcs.storage_paths.resolve_base("tlogs")``, newest first.  Tapping a
file dismisses the popup and calls ``on_selected(absolute_path)``;
Cancel just dismisses.

This module is a standalone leaf component — wiring it into the UI is
owned by another work stream.
"""

import os
from datetime import datetime

from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.utils import escape_markup

from app.theme import get_color
from gcs.logutil import get_logger
from gcs.storage_paths import resolve_base

log = get_logger("tlog_picker")


def _human_size(num_bytes):
    """Render a byte count as a human-readable B/KB/MB string."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def _list_tlogs(folder):
    """Return ``(entries, error)`` for the ``*.tlog`` files in *folder*.

    ``entries`` is a list of ``(name, abs_path, size_bytes, mtime)``
    tuples sorted newest first.  ``error`` is ``None`` on success, or a
    short description if the folder itself could not be listed.
    """
    try:
        names = os.listdir(folder)
    except OSError as exc:
        log.warning("Could not list tlog folder %s: %s", folder, exc)
        return [], str(exc)

    entries = []
    for name in names:
        if not name.lower().endswith(".tlog"):
            continue
        path = os.path.abspath(os.path.join(folder, name))
        try:
            if not os.path.isfile(path):
                continue
            st = os.stat(path)
        except OSError:
            continue  # vanished or unreadable — skip this one file
        entries.append((name, path, st.st_size, st.st_mtime))

    # Newest first.  Name (descending) breaks same-second ties so the
    # _1/_2 suffixes the tlog writer appends on quick reconnects keep
    # the most recent file on top.
    entries.sort(key=lambda e: (e[3], e[0]), reverse=True)
    return entries, None


def open_tlog_picker(on_selected):
    """Open a popup listing recorded ``.tlog`` files, newest first.

    Tapping a file dismisses the popup and calls
    ``on_selected(absolute_path)``.  Cancel dismisses without selecting.
    """
    folder = resolve_base("TelemetryLog", prefer_removable=True)
    entries, error = _list_tlogs(folder)

    content = BoxLayout(orientation='vertical', padding=10, spacing=8)

    popup = Popup(title='Select Telemetry Log', content=content,
                  size_hint=(0.7, 0.8), auto_dismiss=False)

    if entries:
        content.add_widget(Label(
            text=f"{len(entries)} telemetry log(s) \u2014 newest first",
            font_size='14sp', size_hint_y=None, height=30,
            color=get_color("text_label")))

        scroll = ScrollView(do_scroll_y=True, do_scroll_x=False)
        file_box = BoxLayout(
            orientation='vertical', size_hint_y=None, spacing=6,
            padding=[0, 4, 0, 4])
        file_box.bind(minimum_height=file_box.setter('height'))

        for name, path, size, mtime in entries:
            when = datetime.fromtimestamp(mtime).strftime(
                "%Y-%m-%d %H:%M:%S")
            btn = Button(
                text=(f"[b]{escape_markup(name)}[/b]\n"
                      f"[size={int(sp(11))}]{_human_size(size)}"
                      f"    {when}[/size]"),
                markup=True,
                font_size=sp(13),
                size_hint_y=None, height=56,
                halign='left', valign='middle',
                background_color=list(get_color("bg_input")),
                color=list(get_color("text_primary")))
            btn.bind(size=lambda inst, _val: setattr(
                inst, 'text_size', (inst.width - dp(16), None)))
            btn.bind(on_release=lambda *_a, _p=path: (
                popup.dismiss(), on_selected(_p)))
            file_box.add_widget(btn)

        scroll.add_widget(file_box)
        content.add_widget(scroll)
    else:
        if error is not None:
            msg = (f"Could not read the telemetry log folder:\n\n{folder}"
                   f"\n\n({error})")
        else:
            msg = (f"No telemetry logs (*.tlog) found in:\n\n{folder}"
                   "\n\nConnect to a vehicle to record one.")
        body = Label(
            text=msg, font_size='13sp',
            color=get_color("text_label"),
            halign='center', valign='middle')
        body.bind(size=lambda inst, _val: setattr(
            inst, 'text_size', (inst.width, None)))
        content.add_widget(body)

    btn_row = BoxLayout(size_hint_y=None, height=44, spacing=10)
    cancel_btn = Button(
        text='Cancel', font_size='14sp',
        background_color=list(get_color("btn_clear")))
    cancel_btn.bind(on_release=lambda *_: popup.dismiss())
    btn_row.add_widget(cancel_btn)
    content.add_widget(btn_row)

    popup.open()