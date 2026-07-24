# Localize Dependencies - KiCad 10 IPC action plugin
#
# Makes a KiCad project self-contained by copying its external symbol,
# footprint, and 3D-model dependencies into project-local libraries and
# remapping the references. The project then opens on any machine, even without
# the original global/third-party libraries installed.
#
# The plugin connects over the IPC API (kicad-python / kipy) only to locate the
# open project; the actual work is done on the project files on disk via the
# S-expr editor in portability.py / sexpr.py. Every run is backed up first, so
# Revert can put things back.
#
# License: GPL-3.0-or-later

import os
import re
import sys

import wx

from kipy import KiCad
from kipy.errors import ConnectionError as KiCadConnectionError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portability  # noqa: E402
from _win_dialog import get_foreground_hwnd, attach_to_kicad  # noqa: E402
from _mac_dialog import attach_to_stage_manager, prepare_app  # noqa: E402

TITLE = "Localize Dependencies"

COL_TYPE, COL_REF, COL_NAME, COL_SOURCE, COL_STATUS = range(5)
_COL_TITLES = ("Type", "Reference", "Name", "Source", "Status")
GREY = wx.Colour(140, 140, 140)
# Sort order when sorting by status: the localizable rows come first.
_STATUS_ORDER = {"external": 0, "standard": 1, "missing": 2, "local": 3}
_TYPE_ORDER = {"symbol": 0, "footprint": 1, "model": 2}


def _project_dir(board):
    for getter in (lambda: board.name, lambda: board.get_project().path):
        try:
            cand = getter()
        except Exception:
            cand = None
        if not cand:
            continue
        d = cand if os.path.isdir(cand) else os.path.dirname(cand)
        if d and os.path.isdir(d):
            return os.path.abspath(d)
    return ""


def _natural_key(text):
    key = []
    for chunk in re.split(r"(\d+)", text or ""):
        if chunk.isdigit():
            key.append((1, int(chunk), ""))
        else:
            key.append((0, 0, chunk.lower()))
    return key


def _localizable(row):
    return row["status"] in ("external", "standard")


class LocalizeDialog(wx.Dialog):
    def __init__(self, project_dir, rows):
        super().__init__(None, title=TITLE,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.project_dir = project_dir
        self.rows = rows
        self.sort_col = COL_TYPE
        self.sort_asc = True
        self._populating = False
        # Checked by default: external rows. The "include standard" box adds the
        # standard rows. Selection is tracked by row key so a re-sort keeps it.
        self._checked = {r["key"] for r in rows if r["status"] == "external"}

        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
        if os.path.exists(icon):
            self.SetIcon(wx.Icon(icon, wx.BITMAP_TYPE_PNG))

        self.info = wx.StaticText(self, label="Project: {}".format(project_dir))

        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        try:
            self.list.EnableCheckBoxes(True)
        except Exception:
            pass
        for col, (title, width) in enumerate(zip(
                _COL_TITLES, (70, 90, 240, 380, 90))):
            self.list.InsertColumn(col, title, width=width)
        self.list.Bind(wx.EVT_LIST_COL_CLICK, self.on_col_click)
        for name, handler in (("EVT_LIST_ITEM_CHECKED", self.on_checked),
                              ("EVT_LIST_ITEM_UNCHECKED", self.on_unchecked)):
            evt = getattr(wx, name, None)
            if evt is not None:
                self.list.Bind(evt, handler)

        self.include_std = wx.CheckBox(self, label="Also include standard KiCad libraries")
        self.include_std.Bind(wx.EVT_CHECKBOX, self.on_include_std)
        self.summary = wx.StaticText(self, label="")

        btn_all = wx.Button(self, label="Select all")
        btn_none = wx.Button(self, label="Select none")
        btn_sym = wx.Button(self, label="All symbols")
        btn_fp = wx.Button(self, label="All footprints")
        btn_mod = wx.Button(self, label="All 3D models")
        self.btn_run = wx.Button(self, label="Localize")
        self.btn_revert = wx.Button(self, label="Revert")
        btn_close = wx.Button(self, wx.ID_CANCEL, label="Close")
        btn_all.Bind(wx.EVT_BUTTON, lambda e: self._check_all(True))
        btn_none.Bind(wx.EVT_BUTTON, lambda e: self._check_all(False))
        btn_sym.Bind(wx.EVT_BUTTON, lambda e: self._check_type("symbol"))
        btn_fp.Bind(wx.EVT_BUTTON, lambda e: self._check_type("footprint"))
        btn_mod.Bind(wx.EVT_BUTTON, lambda e: self._check_type("model"))
        self.btn_run.Bind(wx.EVT_BUTTON, self.on_run)
        self.btn_revert.Bind(wx.EVT_BUTTON, self.on_revert)

        left = wx.BoxSizer(wx.HORIZONTAL)
        for b in (btn_all, btn_none, btn_sym, btn_fp, btn_mod):
            left.Add(b, 0, wx.RIGHT, 6)
        right = wx.BoxSizer(wx.HORIZONTAL)
        right.Add(self.btn_run, 0, wx.RIGHT, 8)
        right.Add(self.btn_revert, 0, wx.RIGHT, 8)
        right.Add(btn_close, 0)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.Add(left, 0, wx.ALIGN_CENTER_VERTICAL)
        btns.AddStretchSpacer()
        btns.Add(right, 0, wx.ALIGN_CENTER_VERTICAL)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.info, 0, wx.ALL, 10)
        outer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        outer.Add(self.include_std, 0, wx.ALL, 10)
        outer.Add(self.summary, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        outer.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(outer)

        self.apply_sort()
        self.populate()
        self.btn_revert.Enable(portability.has_backup(project_dir))
        self.SetSize((980, 560))
        self.SetMinSize((900, 400))

    # --- population / sorting ---

    def populate(self):
        self._populating = True
        self.list.DeleteAllItems()
        for i, row in enumerate(self.rows):
            self.list.InsertItem(i, row["type"])
            self.list.SetItem(i, COL_REF, row["ref"])
            self.list.SetItem(i, COL_NAME, row["name"])
            self.list.SetItem(i, COL_SOURCE, row["source"] or "")
            self.list.SetItem(i, COL_STATUS, self._status_label(row))
            if _localizable(row):
                try:
                    self.list.CheckItem(i, row["key"] in self._checked)
                except Exception:
                    pass
            else:
                self.list.SetItemTextColour(i, GREY)
        self._populating = False
        self._update_headers()
        self._update_summary()

    def _status_label(self, row):
        return {"external": "external", "standard": "standard lib",
                "local": "already local", "missing": "MISSING"}.get(
                    row["status"], row["status"])

    def _update_summary(self):
        n_local = sum(1 for r in self.rows if _localizable(r))
        n_checked = sum(1 for r in self.rows if _localizable(r) and r["key"] in self._checked)
        by = {"symbol": 0, "footprint": 0, "model": 0}
        for r in self.rows:
            if _localizable(r) and r["key"] in self._checked:
                by[r["type"]] += 1
        self.summary.SetLabel(
            "{} of {} localizable items selected  "
            "({} symbols, {} footprints, {} models)".format(
                n_checked, n_local, by["symbol"], by["footprint"], by["model"]))

    def _update_headers(self):
        for i, base in enumerate(_COL_TITLES):
            col = self.list.GetColumn(i)
            if not col:
                continue
            col.SetMask(col.GetMask() | wx.LIST_MASK_TEXT)
            arrow = ("  ▲" if self.sort_asc else "  ▼") if i == self.sort_col else ""
            col.SetText(base + arrow)
            self.list.SetColumn(i, col)

    def on_col_click(self, evt):
        col = evt.GetColumn()
        if col == self.sort_col:
            self.sort_asc = not self.sort_asc
        else:
            self.sort_col = col
            self.sort_asc = True
        self.apply_sort()
        self.populate()

    def apply_sort(self):
        def key(row):
            ref = _natural_key(row["ref"])
            if self.sort_col == COL_REF:
                primary = ref
            elif self.sort_col == COL_NAME:
                primary = row["name"].lower()
            elif self.sort_col == COL_SOURCE:
                primary = (row["source"] or "").lower()
            elif self.sort_col == COL_STATUS:
                primary = _STATUS_ORDER.get(row["status"], 9)
            else:
                primary = _TYPE_ORDER.get(row["type"], 9)
            return (primary, _TYPE_ORDER.get(row["type"], 9), ref)
        self.rows.sort(key=key, reverse=not self.sort_asc)

    # --- checkbox state ---

    def on_checked(self, evt):
        self._set_checked(evt.GetIndex(), True)

    def on_unchecked(self, evt):
        self._set_checked(evt.GetIndex(), False)

    def _set_checked(self, index, value):
        if self._populating or not (0 <= index < len(self.rows)):
            return
        row = self.rows[index]
        if not _localizable(row):
            # Non-localizable rows can't be selected; undo the visual check.
            try:
                self.list.CheckItem(index, False)
            except Exception:
                pass
            return
        if value:
            self._checked.add(row["key"])
        else:
            self._checked.discard(row["key"])
        self._update_summary()

    def _check_all(self, value):
        for row in self.rows:
            if _localizable(row):
                if value:
                    self._checked.add(row["key"])
                else:
                    self._checked.discard(row["key"])
        self.populate()

    def _check_type(self, type_key):
        """Check every localizable row of one type, leaving the rest as they are.
        Pair with 'Select none' to select only that type."""
        for row in self.rows:
            if _localizable(row) and row["type"] == type_key:
                self._checked.add(row["key"])
        self.populate()

    def on_include_std(self, _evt):
        want = self.include_std.GetValue()
        for row in self.rows:
            if row["status"] == "standard":
                if want:
                    self._checked.add(row["key"])
                else:
                    self._checked.discard(row["key"])
        self.populate()

    # --- actions ---

    def _selected_rows(self):
        return [r for r in self.rows if _localizable(r) and r["key"] in self._checked]

    def _msg(self, text, style):
        return wx.MessageBox(text, TITLE, style, self)

    def on_run(self, _evt):
        selected = self._selected_rows()
        if not selected:
            self._msg("Tick at least one item to localize.", wx.OK | wx.ICON_INFORMATION)
            return
        if self._msg(
            "This edits the project files on disk.\n\n"
            "1. Save the project in KiCad first (Ctrl+S).\n"
            "2. This localizes the ticked items and backs up every file it\n"
            "   changes (Revert undoes everything).\n"
            "3. Afterwards, close the Schematic and PCB editors WITHOUT saving,\n"
            "   then reopen the project so KiCad reads the localized files.\n\n"
            "Continue?",
            wx.YES_NO | wx.ICON_WARNING,
        ) != wx.YES:
            return

        busy = wx.BusyCursor()
        try:
            session = portability.Session(self.project_dir)
            result = portability.apply_selection(session, selected)
            session.save_manifest(result)
        except Exception as exc:
            del busy
            self._msg("Localize failed (no changes kept):\n{}".format(exc),
                      wx.OK | wx.ICON_ERROR)
            return
        del busy

        parts = []
        if result.get("symbols"):
            r = result["symbols"]
            parts.append("Symbols: {} libraries, {} references".format(
                r["libraries"], r["remapped"]))
        if result.get("footprints"):
            r = result["footprints"]
            parts.append("Footprints: {} libraries, {} files, {} references".format(
                r["libraries"], r["files_copied"], r["remapped"]))
        if result.get("models"):
            r = result["models"]
            parts.append("3D models: {} files, {} references".format(
                r["files_copied"], r["remapped"]))

        self.refresh()
        self._msg("Localized:\n\n" + "\n".join(parts) +
                  "\n\nTo see the changes, close the Schematic and PCB editors "
                  "without saving, then reopen the project.",
                  wx.OK | wx.ICON_INFORMATION)

    def on_revert(self, _evt):
        if self._msg(
            "Revert all localized dependencies back to their originals?\n"
            "This restores every changed file and removes the local copies.\n\n"
            "Afterwards, close the editors without saving and reopen the project.",
            wx.YES_NO | wx.ICON_QUESTION,
        ) != wx.YES:
            return
        busy = wx.BusyCursor()
        try:
            res = portability.revert(self.project_dir)
        except Exception as exc:
            del busy
            self._msg("Revert failed:\n{}".format(exc), wx.OK | wx.ICON_ERROR)
            return
        del busy
        self.refresh()
        msg = "Restored {} file(s), removed {} local file(s).".format(
            res["restored"], res["removed"])
        if res["errors"]:
            msg += "\n\nErrors:\n" + "\n".join(res["errors"][:20])
        msg += ("\n\nClose the Schematic and PCB editors without saving, then "
                "reopen the project to see the changes.")
        self._msg(msg, wx.OK | (wx.ICON_WARNING if res["errors"] else wx.ICON_INFORMATION))

    def refresh(self):
        busy = wx.BusyCursor()  # spinning cursor while the project is re-scanned
        self.rows = portability.scan_items(self.project_dir, self.include_std.GetValue())
        self._checked = {r["key"] for r in self.rows if r["status"] == "external"}
        if self.include_std.GetValue():
            self._checked |= {r["key"] for r in self.rows if r["status"] == "standard"}
        self.apply_sort()
        self.populate()
        self.btn_revert.Enable(portability.has_backup(self.project_dir))
        del busy


def main():
    foreground_hwnd = get_foreground_hwnd()
    app = wx.App()  # noqa: F841
    prepare_app()

    try:
        kicad = KiCad()
        board = kicad.get_board()
    except KiCadConnectionError:
        wx.MessageBox(
            "Could not connect to KiCad.\n\n"
            "Enable the API server in Preferences > Plugins, and make sure a "
            "board is open in the PCB editor.",
            TITLE, wx.OK | wx.ICON_ERROR,
        )
        return

    project_dir = _project_dir(board)
    if not project_dir:
        wx.MessageBox(
            "Could not determine the project directory.\n"
            "Save the project at least once, then run the plugin again.",
            TITLE, wx.OK | wx.ICON_ERROR,
        )
        return

    # Spinning cursor stays up through the scan AND the dialog build, and is only
    # released the moment before the window is shown.
    busy = wx.BusyCursor()
    try:
        rows = portability.scan_items(project_dir)
    except Exception as exc:
        del busy
        wx.MessageBox("Could not read the project:\n{}".format(exc),
                      TITLE, wx.OK | wx.ICON_ERROR)
        return

    dlg = LocalizeDialog(project_dir, rows)
    attach_to_kicad(dlg, foreground_hwnd, board)
    attach_to_stage_manager(dlg)
    dlg.CentreOnScreen()
    del busy
    try:
        dlg.ShowModal()
    finally:
        dlg.Destroy()


if __name__ == "__main__":
    main()
