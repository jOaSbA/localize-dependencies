# Windows dialog-ownership helpers.
#
# An IPC plugin runs in its own process, so a wx dialog it opens is, to Windows,
# a top-level window belonging to a different application: it gets its own
# taskbar button and can end up hidden behind the KiCad editor. Making the dialog
# an owned window of the KiCad PCB editor fixes both problems: no taskbar button,
# and it stays attached to (and on top of) KiCad the way an in-process plugin
# dialog would.
#
# These helpers are no-ops on non-Windows platforms, where wx already parents
# dialogs sensibly.
#
# License: GPL-3.0-or-later

import os
import sys


def get_foreground_hwnd():
    """Handle of the currently focused window, normally the KiCad editor the user
    just clicked in. Capture it before you create any window of your own."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return None


def resolve_kicad_hwnd(foreground_hwnd, board):
    """Find the KiCad PCB editor's top-level window to own our dialog to."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        def title(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value

        try:
            base = os.path.splitext(os.path.basename(board.name))[0]
        except Exception:
            base = ""

        # The foreground window is usually the editor; trust it if it looks right.
        if foreground_hwnd:
            t = title(foreground_hwnd)
            if t and ("PCB Editor" in t or (base and base in t)):
                return foreground_hwnd

        matches = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                t = title(hwnd)
                if t and ("PCB Editor" in t or (base and base in t)):
                    matches.append(hwnd)
            return True

        user32.EnumWindows(proto(callback), 0)
        if foreground_hwnd in matches:
            return foreground_hwnd
        if matches:
            return matches[0]
        return foreground_hwnd or None
    except Exception:
        return None


def own_to_hwnd(window, owner_hwnd):
    """Make `window` an owned window of `owner_hwnd` (which may live in another
    process). Owned windows get no taskbar button and follow their owner.
    Returns True on success."""
    if sys.platform != "win32" or not owner_hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        GWLP_HWNDPARENT = -8
        user32 = ctypes.windll.user32
        set_owner = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        set_owner.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        set_owner.restype = ctypes.c_void_p
        set_owner(window.GetHandle(), GWLP_HWNDPARENT, owner_hwnd)
        return True
    except Exception:
        return False


def make_tool_window(window):
    """Fallback when the KiCad window can't be found: at least keep the dialog
    out of the taskbar by marking it a tool window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        user32 = ctypes.windll.user32
        hwnd = window.GetHandle()
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        )
    except Exception:
        pass


def attach_to_kicad(window, foreground_hwnd, board):
    """Own `window` to the KiCad editor if possible, else fall back to a tool
    window. Convenience wrapper over the helpers above."""
    kicad_hwnd = resolve_kicad_hwnd(foreground_hwnd, board)
    if not own_to_hwnd(window, kicad_hwnd):
        make_tool_window(window)
