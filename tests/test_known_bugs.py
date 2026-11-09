# Open bugs, each written as the behavior we want. Every test here fails until
# its bug is fixed; then move it into test_engine.py (and CI). Not run in CI,
# so the suite stays green while these are open.
#
# Run: python tests/test_known_bugs.py   (prints FAIL per open bug, exit 1)
#
# License: GPL-3.0-or-later

import os
import sys
import traceback

from _util import env, fp_project, fwd, localize, portability, read, rows, \
    table, tmp, write
import sexpr


def test_unforced_revert_keeps_what_a_kept_file_needs():
    # The board is re-saved after localize, so an unforced revert skips it.
    # It still says Lib_local:R, so its library must survive the revert.
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n')
        localize(proj, rows(proj, "footprint"))
        pcb = os.path.join(proj, "board.kicad_pcb")
        write(pcb, read(pcb).replace("R1", "R9"))
        portability.revert(proj)
        got = [(r["name"], r["status"]) for r in rows(proj, "footprint")]
        assert got == [("Lib_local:R", "local")], got


def test_footprint_missing_from_its_library_is_not_remapped():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:Gone" (property "Reference" "U1"))\n',
                          libs=(("Lib", ()),))
        r = rows(proj, "footprint")
        localize(proj, [x for x in r if x["wanted"]])
        assert '"Lib:Gone"' in read(os.path.join(proj, "board.kicad_pcb")), \
            "remapped to a local library that does not hold the footprint"


def test_legacy_symbol_library_is_not_offered():
    # A .lib copied to a .kicad_sym and registered as type KiCad will not load.
    with tmp() as root, env():
        proj = os.path.join(root, "proj")
        leg = os.path.join(root, "old.lib")
        write(leg, "EESchema-LIBRARY Version 2.4\nDEF R R 0 0 N Y 1 F N\nENDDEF\n")
        write(os.path.join(proj, "sym-lib-table"),
              table("sym", ("Old", "Legacy", fwd(leg))))
        write(os.path.join(proj, "a.kicad_sch"),
              '(kicad_sch (lib_symbols (symbol "Old:R")) '
              '(symbol (lib_id "Old:R") (property "Reference" "R1")))\n')
        r = rows(proj, "symbol")[0]
        assert not r["wanted"], r


def test_existing_local_nickname_is_not_hijacked():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n')
        other = os.path.join(root, "ext", "Other.pretty")
        write(os.path.join(other, "R.kicad_mod"), '(footprint "other R")\n')
        write(os.path.join(proj, "fp-lib-table"), table(
            "fp", ("Lib", "KiCad", fwd(os.path.join(root, "ext", "Lib.pretty"))),
            ("Lib_local", "KiCad", fwd(other))))
        localize(proj, rows(proj, "footprint"))
        name = rows(proj, "footprint")[0]["name"]
        uri = portability.resolve_lib_uri(proj, "fp-lib-table", name.split(":")[0])
        assert uri.startswith("${KIPRJMOD}/libs/"), (name, uri)


def test_schematic_footprint_field_follows_the_board():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n')
        sch = os.path.join(proj, "board.kicad_sch")
        write(sch, '(kicad_sch (symbol (lib_id "Device:R") (property "Reference" "R1") '
                   '(property "Footprint" "Lib:R")))\n')
        localize(proj, rows(proj, "footprint"))
        sym = sexpr.parse(read(sch)).find("symbol")
        assert portability._prop(sym, "Footprint") == "Lib_local:R", \
            "Update PCB from Schematic would undo the localize"


def test_sheet_in_a_subfolder_is_scanned_and_remapped():
    with tmp() as root, env():
        proj = os.path.join(root, "proj")
        lib = os.path.join(root, "ext", "My.kicad_sym")
        write(lib, '(kicad_symbol_lib (symbol "R"))\n')
        write(os.path.join(proj, "sym-lib-table"), table("sym", ("My", "KiCad", fwd(lib))))
        write(os.path.join(proj, "a.kicad_sch"),
              '(kicad_sch (lib_symbols (symbol "My:R")) '
              '(symbol (lib_id "My:R") (property "Reference" "R1")) '
              '(sheet (property "Sheetfile" "sub/s.kicad_sch")))\n')
        sub = os.path.join(proj, "sub", "s.kicad_sch")
        write(sub, '(kicad_sch (lib_symbols (symbol "My:R")) '
                   '(symbol (lib_id "My:R") (property "Reference" "R2")))\n')
        assert sorted(r["ref"] for r in rows(proj, "symbol")) == ["R1", "R2"]
        localize(proj, [r for r in rows(proj, "symbol") if r["wanted"]])
        assert '"My_local:R"' in read(sub) and '"My:R"' not in read(sub)


def test_project_relative_model_paths_are_local():
    with tmp() as root, env():
        write(os.path.join(root, "models", "R.step"), "x")
        for raw in ("$(KIPRJMOD)/models/R.step", "models/R.step", "kicad-embed://R.step"):
            got = portability.classify(raw, root)
            assert got == "local", (raw, got)


def test_autosave_files_are_ignored():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n')
        write(os.path.join(proj, "_autosave-board.kicad_pcb"),
              '(kicad_pcb (footprint "Lib:R" (property "Reference" "R1")))\n')
        files = {r["key"][1] for r in rows(proj)}
        assert files == {"board.kicad_pcb"}, files


def test_standard_check_is_a_path_prefix_not_a_substring():
    with tmp() as root, env():
        share = os.path.join(root, "share", "kicad")
        os.makedirs(os.path.join(share, "footprints"))
        extra = os.path.join(root, "share", "kicad-extra-libs", "X.pretty")
        os.makedirs(extra)
        portability._INSTALL_GLOBS = (share,)
        assert portability.classify_lib(fwd(extra), root) == "external"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print("FIXED", name, "(move it to test_engine.py)")
        except Exception:
            failed += 1
            print("FAIL ", name)
            print("       " + traceback.format_exc().strip().splitlines()[-1])
    sys.exit(1 if failed else 0)
