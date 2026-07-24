# macOS dialog integration for Stage Manager.
#
# An IPC plugin runs in its own process, so macOS normally treats its wx dialog
# as a separate application and pushes the PCB editor into the background.
#
# License: GPL-3.0-or-later

import platform
import sys


def _objc_runtime():
    import ctypes

    objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    return ctypes, objc


def prepare_app():
    """Make the wx process an accessory app before creating any windows."""
    if sys.platform != "darwin":
        return
    try:
        ctypes, objc = _objc_runtime()
        send = objc.objc_msgSend
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        send.restype = ctypes.c_void_p
        app = send(
            objc.objc_getClass(b"NSApplication"),
            objc.sel_registerName(b"sharedApplication"),
        )

        # NSApplicationActivationPolicyAccessory = 1. It can receive focus but
        # does not present itself as an independent Dock application.
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send.restype = ctypes.c_bool
        send(app, objc.sel_registerName(b"setActivationPolicy:"), 1)
    except Exception:
        # Native integration is cosmetic; do not prevent the plugin from
        # running if a future wxPython/AppKit combination changes its handles.
        pass


def attach_to_stage_manager(window):
    """Keep a wx dialog visible in the PCB editor's Stage Manager set."""
    if sys.platform != "darwin":
        return
    try:
        ctypes, objc = _objc_runtime()
        send = objc.objc_msgSend

        # wxWindow::GetHandle() is an NSView on wxOSX. NSWindow also responds
        # to -window by returning itself, which covers either native handle.
        native_handle = ctypes.c_void_p(int(window.GetHandle()))
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        send.restype = ctypes.c_void_p
        ns_window = send(native_handle, objc.sel_registerName(b"window"))
        if not ns_window:
            return

        # NSFloatingWindowLevel. Keep it visible when focus returns to KiCad.
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send.restype = None
        send(ns_window, objc.sel_registerName(b"setLevel:"), 3)
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        send(ns_window, objc.sel_registerName(b"setHidesOnDeactivate:"), False)

        # This Stage Manager behavior was introduced with macOS 13. Apple
        # defines it for floating windows that should join other apps' sets.
        major = int(platform.mac_ver()[0].split(".")[0] or 0)
        if major >= 13:
            transient = 1 << 3
            ignores_cycle = 1 << 6
            can_join_all_applications = 1 << 18

            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            send.restype = ctypes.c_ulong
            behavior = send(
                ns_window, objc.sel_registerName(b"collectionBehavior")
            )
            # Primary and Auxiliary are mutually exclusive with
            # CanJoinAllApplications.
            behavior &= ~((1 << 16) | (1 << 17))
            behavior |= transient | ignores_cycle | can_join_all_applications
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            send.restype = None
            send(
                ns_window,
                objc.sel_registerName(b"setCollectionBehavior:"),
                behavior,
            )
    except Exception:
        pass
