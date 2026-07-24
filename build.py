#!/usr/bin/env python3
"""Build the PCM release archive for this plugin.

Run from the repository root:

    python build.py

It produces ``dist/<repo>-<version>.zip`` laid out the way the KiCad Plugin and
Content Manager expects (``plugins/``, ``resources/icon.png``, ``metadata.json``),
then fills ``download_sha256``, ``download_size`` and ``install_size`` back into
the top-level ``metadata.json`` for the latest version -- that file is what you
submit to https://gitlab.com/kicad/addons/metadata.

The copy of ``metadata.json`` placed *inside* the archive has the download_*
fields stripped, as required for packaged metadata.
"""

import copy
import hashlib
import json
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.basename(HERE)
DIST = os.path.join(HERE, "dist")

# Files/dirs shipped in the archive, relative to the repo root.
INCLUDE_DIRS = ("plugins", "resources")
# Never ship these.
EXCLUDE_NAMES = {"__pycache__", ".DS_Store"}
EXCLUDE_EXTS = (".pyc", ".pyo")


def _collect():
    """Yield (abs_path, arcname) for every file to place in the archive."""
    for top in INCLUDE_DIRS:
        base = os.path.join(HERE, top)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
            for fn in files:
                if fn in EXCLUDE_NAMES or fn.endswith(EXCLUDE_EXTS):
                    continue
                abs_path = os.path.join(root, fn)
                arcname = os.path.relpath(abs_path, HERE).replace(os.sep, "/")
                yield abs_path, arcname


def main():
    meta_path = os.path.join(HERE, "metadata.json")
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)

    latest = meta["versions"][-1]
    version = latest["version"]

    os.makedirs(DIST, exist_ok=True)
    zip_path = os.path.join(DIST, "{}-{}.zip".format(REPO, version))

    # metadata.json inside the archive: only the version being packaged, with the
    # download_* fields stripped. PCM requires the in-package metadata to contain
    # exactly one version.
    packaged = copy.deepcopy(meta)
    built = copy.deepcopy(latest)
    for k in ("download_url", "download_sha256", "download_size", "install_size"):
        built.pop(k, None)
    packaged["versions"] = [built]

    install_size = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in sorted(_collect(), key=lambda p: p[1]):
            zf.write(abs_path, arcname)
            install_size += os.path.getsize(abs_path)
        packaged_bytes = json.dumps(packaged, indent=2).encode("utf-8")
        zf.writestr("metadata.json", packaged_bytes)
        install_size += len(packaged_bytes)

    with open(zip_path, "rb") as fh:
        data = fh.read()
    sha256 = hashlib.sha256(data).hexdigest()
    download_size = len(data)

    latest["download_sha256"] = sha256
    latest["download_size"] = download_size
    latest["install_size"] = install_size
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")

    print("Built {}".format(zip_path))
    print("  version        {}".format(version))
    print("  download_size  {} bytes".format(download_size))
    print("  install_size   {} bytes".format(install_size))
    print("  download_sha256 {}".format(sha256))
    print()
    print("metadata.json updated. Attach the zip to the GitHub release at:")
    print("  {}".format(latest.get("download_url", "<set download_url in metadata.json>")))


if __name__ == "__main__":
    main()
