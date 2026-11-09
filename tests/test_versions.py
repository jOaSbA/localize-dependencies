# With several KiCad versions installed side by side, the plugin must use the
# newest one's libraries and library tables. A string sort ranks "9.0" and
# "6.0" above "10.0", which silently copied KiCad 6 footprints into a
# KiCad 10 project.
#
# Run: python tests/test_versions.py   (or pytest)
#
# License: GPL-3.0-or-later

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
import portability  # noqa: E402

VERSIONS = ("6.0", "9.0", "10.0", "8.0")


def test_install_share_picks_newest():
    root = tempfile.mkdtemp()
    real = portability._INSTALL_GLOBS
    try:
        for v in VERSIONS:
            os.makedirs(os.path.join(root, v, "share", "kicad"))
        portability._INSTALL_GLOBS = (os.path.join(root, "*", "share", "kicad"),)
        assert portability._install_share() == os.path.join(root, "10.0", "share", "kicad")
    finally:
        portability._INSTALL_GLOBS = real
        shutil.rmtree(root)


def test_config_dirs_newest_first():
    root = tempfile.mkdtemp()
    var = "APPDATA" if os.name == "nt" else "HOME"
    base = os.path.join(root, "kicad") if os.name == "nt" else \
        os.path.join(root, ".config", "kicad")
    saved = dict(os.environ)
    try:
        for v in VERSIONS:
            os.makedirs(os.path.join(base, v))
        # these override the platform default (CI runners set XDG_CONFIG_HOME)
        os.environ.pop("KICAD_CONFIG_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ[var] = root
        got = [os.path.basename(d) for d in portability._config_dirs()]
        assert got == ["10.0", "9.0", "8.0", "6.0"], got
    finally:
        os.environ.clear()
        os.environ.update(saved)
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
