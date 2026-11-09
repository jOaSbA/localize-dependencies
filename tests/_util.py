# Shared helpers for the engine tests.
#
# License: GPL-3.0-or-later

import contextlib
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
import portability  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)


def read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def fwd(path):
    """KiCad files store paths with forward slashes; a backslash would be read
    as an escape inside a quoted string."""
    return path.replace("\\", "/")


def table(kind, *libs):
    """Lib table text. libs: (name, type, uri) tuples."""
    rows = "".join('\t(lib (name "{}")(type "{}")(uri "{}")(options "")(descr ""))\n'
                   .format(n, t, u) for n, t, u in libs)
    return "({}\n\t(version 7)\n{})\n".format(
        "fp_lib_table" if kind == "fp" else "sym_lib_table", rows)


@contextlib.contextmanager
def env(**values):
    """Run with no KiCad variables set except `values`, and no install dirs
    unless a test patches _INSTALL_GLOBS itself. Without KICAD_CONFIG_HOME in
    `values`, global tables come from an empty folder, never the developer's
    own KiCad setup. Restores everything."""
    saved_env = dict(os.environ)
    saved_globs = portability._INSTALL_GLOBS
    for key in list(os.environ):
        if re.fullmatch(r"KICAD\d*_\w+", key.upper()) or \
                key.upper() in ("KICAD_CONFIG_HOME", "XDG_CONFIG_HOME"):
            del os.environ[key]
    os.environ.setdefault("KICAD_CONFIG_HOME",
                          os.path.join(tempfile.gettempdir(), "no-kicad-config"))
    os.environ.update(values)
    portability._INSTALL_GLOBS = ()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        portability._INSTALL_GLOBS = saved_globs


@contextlib.contextmanager
def tmp():
    root = tempfile.mkdtemp()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def localize(proj, rows):
    s = portability.Session(proj)
    s.save_manifest(portability.apply_selection(s, rows))


def rows(proj, type_=None, include_standard=False):
    return [r for r in portability.scan_items(proj, include_standard)
            if type_ is None or r["type"] == type_]


def fp_project(root, board_body, libs=(("Lib", ("R",)),)):
    """Project with a board and external footprint libraries.
    libs: (nickname, footprint names). Returns the project dir."""
    proj = os.path.join(root, "proj")
    entries = []
    for nick, names in libs:
        d = os.path.join(root, "ext", nick + ".pretty")
        for n in names:
            write(os.path.join(d, n + ".kicad_mod"), '(footprint "{}")\n'.format(n))
        os.makedirs(d, exist_ok=True)
        entries.append((nick, "KiCad", fwd(d)))
    write(os.path.join(proj, "fp-lib-table"), table("fp", *entries))
    write(os.path.join(proj, "board.kicad_pcb"), "(kicad_pcb\n" + board_body + ")\n")
    return proj


def run_all(namespace):
    for name, fn in sorted(namespace.items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
