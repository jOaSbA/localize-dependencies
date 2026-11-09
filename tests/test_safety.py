# Data-safety checks for the portability engine: undo survives repeat runs,
# copies never overwrite, failed runs are revertible, line endings survive,
# and Revert does not silently discard later edits.
#
# Run: python tests/test_safety.py   (or pytest)

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
import portability  # noqa: E402


def _write(path, data, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kw = {} if "b" in mode else {"encoding": "utf-8", "newline": ""}
    with open(path, mode, **kw) as fh:
        fh.write(data)


def _bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _project(nl="\n", bom=False):
    """Temp project: board with two footprints from external lib 'Lib', each
    with a 3D model. The two models share a file name but come from different
    folders with different content. Returns (root, project_dir)."""
    root = tempfile.mkdtemp()
    proj = os.path.join(root, "proj")
    a = os.path.join(root, "extA", "Parts.3dshapes", "R.step").replace("\\", "/")
    b = os.path.join(root, "extB", "Parts.3dshapes", "R.step").replace("\\", "/")
    _write(a, "model A")
    _write(os.path.join(root, "extA", "Parts.3dshapes", "R.wrl"), "wrl A")
    _write(b, "model B")
    lib = os.path.join(root, "Lib.pretty").replace("\\", "/")
    _write(os.path.join(lib, "R.kicad_mod"), '(footprint "R")\n')
    _write(os.path.join(proj, "fp-lib-table"),
           ('(fp_lib_table\n\t(version 7)\n'
            '\t(lib (name "Lib")(type "KiCad")(uri "{}")(options "")(descr ""))\n'
            ')\n').format(lib).replace("\n", nl))
    pcb = ("(kicad_pcb\n"
           '\t(footprint "Lib:R" (property "Reference" "R1") (model "{}"))\n'
           '\t(footprint "Lib:R" (property "Reference" "R2") (model "{}"))\n'
           ")\n").format(a, b).replace("\n", nl)
    _write(os.path.join(proj, "board.kicad_pcb"), ("﻿" if bom else "") + pcb)
    return root, proj


def _rows(proj, type_, ref=None):
    return [r for r in portability.scan_items(proj)
            if r["type"] == type_ and (ref is None or r["ref"] == ref)]


def _run(proj, rows):
    s = portability.Session(proj)
    s.save_manifest(portability.apply_selection(s, rows))


def test_second_run_keeps_first_undo():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        original = _bytes(pcb)
        _run(proj, _rows(proj, "model", "R1"))
        _run(proj, _rows(proj, "model", "R2"))
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert _bytes(pcb) == original, "revert must reach the pre-first-run state"
        assert not os.path.exists(os.path.join(proj, "packages3D"))
        assert not os.path.exists(os.path.join(proj, ".portability"))
    finally:
        shutil.rmtree(root)


def test_copies_never_overwrite():
    root, proj = _project()
    try:
        mine = os.path.join(proj, "packages3D", "Parts.3dshapes", "R.step")
        _write(mine, "the user's own model")
        _run(proj, _rows(proj, "model"))
        d = os.path.dirname(mine)
        assert _bytes(mine) == b"the user's own model"
        # A and B collide with the user's file and with each other: both renamed
        got = {n: _bytes(os.path.join(d, n)) for n in os.listdir(d)}
        assert got["R_1.step"] == b"model A" and got["R_1.wrl"] == b"wrl A", got
        assert got["R_2.step"] == b"model B", got
        text = _bytes(os.path.join(proj, "board.kicad_pcb")).decode()
        assert "${KIPRJMOD}/packages3D/Parts.3dshapes/R_1.step" in text
        assert "${KIPRJMOD}/packages3D/Parts.3dshapes/R_2.step" in text
        portability.revert(proj)
        assert _bytes(mine) == b"the user's own model", "revert deleted a user file"
    finally:
        shutil.rmtree(root)


def test_failed_run_is_revertible():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        table = os.path.join(proj, "fp-lib-table")
        before = (_bytes(pcb), _bytes(table))
        rows = [r for r in portability.scan_items(proj) if r["wanted"]]
        assert {r["type"] for r in rows} == {"footprint", "model"}, rows

        real = portability._place_model

        def boom(*a, **k):
            raise RuntimeError("disk full")
        portability._place_model = boom
        try:
            s = portability.Session(proj)
            try:
                portability.apply_selection(s, rows)   # footprints done, models die
                assert False, "expected failure"
            except RuntimeError:
                pass
        finally:
            portability._place_model = real

        assert _bytes(pcb) != before[0], "footprints should have been remapped"
        assert portability.has_backup(proj), "failed run left no undo"
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert (_bytes(pcb), _bytes(table)) == before
        assert not os.path.exists(os.path.join(proj, "libs"))
    finally:
        shutil.rmtree(root)


def test_crlf_and_bom_survive():
    root, proj = _project(nl="\r\n", bom=True)
    try:
        _run(proj, [r for r in portability.scan_items(proj) if r["wanted"]])
        for name in ("board.kicad_pcb", "fp-lib-table"):
            data = _bytes(os.path.join(proj, name))
            assert data.count(b"\n") == data.count(b"\r\n"), name + " lost CRLF"
        assert _bytes(os.path.join(proj, "board.kicad_pcb")).startswith(b"\xef\xbb\xbf")
    finally:
        shutil.rmtree(root)


def test_revert_keeps_later_edits_unless_forced():
    root, proj = _project()
    try:
        pcb = os.path.join(proj, "board.kicad_pcb")
        original = _bytes(pcb)
        _run(proj, _rows(proj, "model"))
        edited = _bytes(pcb).replace(b"R1", b"R9")
        _write(pcb, edited, "wb")
        assert portability.revert_conflicts(proj) == ["board.kicad_pcb"]

        res = portability.revert(proj)
        assert _bytes(pcb) == edited, "unforced revert overwrote a later edit"
        assert any("board.kicad_pcb" in e for e in res["errors"])
        # the kept board still points at the local models, so they stay
        assert os.path.exists(os.path.join(proj, "packages3D", "Parts.3dshapes", "R.step"))

        res = portability.revert(proj, force=True)
        assert res["errors"] == [], res
        assert _bytes(pcb) == original
        assert not os.path.exists(os.path.join(proj, "packages3D"))
        assert not os.path.exists(os.path.join(proj, ".portability"))
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
