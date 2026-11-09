# Engine behavior that has to hold on every release: the S-expression editor
# never touches a byte it was not asked to, library tables stay consistent
# across runs, and localization moves exactly what was selected.
#
# Run: python tests/test_engine.py   (or pytest)
#
# License: GPL-3.0-or-later

import os
import stat

from _util import env, fp_project, fwd, localize, portability, read, rows, \
    run_all, table, tmp, write
import sexpr


# --- sexpr: parse and edit --------------------------------------------------------

TRICKY = ('\ufeff(kicad_pcb (version 20240108)\r\n'
          '\t(gr_text "a \\"quoted\\" (paren) \\\\ back" (at 1 2))\r\n'
          '\t(property "Value" "µF 中文 ünïcode")\r\n'
          '\t(footprint "Lib:R" (model "C:/x y/(odd) name.step"))\r\n'
          ')\r\n')


def test_parse_keeps_structure_with_parens_and_quotes_in_strings():
    root = sexpr.parse(TRICKY)
    assert root.head() == "kicad_pcb"
    assert [c.head() for c in root.children[1:]] == \
        ["version", "gr_text", "property", "footprint"]
    assert root.find("gr_text").children[1].value == 'a "quoted" (paren) \\ back'
    assert root.find("property").children[2].value == "µF 中文 ünïcode"
    model = root.find("footprint").find("model")
    assert model.children[1].value == "C:/x y/(odd) name.step"


def test_one_edit_changes_only_its_span():
    root = sexpr.parse(TRICKY)
    atom = root.find("footprint").children[1]
    ed = sexpr.Editor(TRICKY)
    ed.replace_atom(atom, "Lib_local:R")
    out = ed.result()
    assert out == TRICKY[:atom.start] + '"Lib_local:R"' + TRICKY[atom.end:]
    assert out.startswith("\ufeff") and out.count("\r\n") == TRICKY.count("\r\n")
    sexpr.parse(out)


def test_several_edits_in_any_order():
    text = '(a "one" "two" "three")'
    root = sexpr.parse(text)
    ed = sexpr.Editor(text)
    ed.replace_atom(root.children[3], "3")
    ed.replace_atom(root.children[1], "1")
    ed.replace_atom(root.children[2], "a longer two")
    assert ed.result() == '(a "1" "a longer two" "3")'


def test_unquoted_atom_stays_unquoted_unless_forced():
    text = "(a b)"
    atom = sexpr.parse(text).children[1]
    ed = sexpr.Editor(text)
    ed.replace_atom(atom, "c")
    assert ed.result() == "(a c)"
    ed = sexpr.Editor(text)
    ed.replace_atom(atom, "c d", force_quote=True)
    assert ed.result() == '(a "c d")'


def test_quote_then_parse_round_trips():
    for s in ('plain', 'with "quotes"', "back\\slash", "C:\\dir\\", "tab\there",
              "new\nline", "cr\rlf", "(parens)", "µ 中文", "", "trailing \\"):
        assert sexpr.parse("(a {})".format(sexpr.quote(s))).children[1].value == s, s


def test_malformed_input_is_rejected():
    for bad in ("", "   ", "a", "(a", "(a))", '(a "open)', "(a) b", "(a (b)"):
        try:
            sexpr.parse(bad)
        except sexpr.SExprError:
            continue
        raise AssertionError("accepted malformed input: {!r}".format(bad))


def test_find_and_find_all_are_direct_children_only():
    root = sexpr.parse("(r (x 1) (y (x 2)) (x 3))")
    assert [n.children[1].value for n in root.find_all("x")] == ["1", "3"]
    assert root.find("y").find("x").children[1].value == "2"
    assert root.find("z") is None
    assert len([n for n in root.iter_lists() if n.head() == "x"]) == 3


# --- library tables ---------------------------------------------------------------

def test_two_runs_on_one_library_add_one_table_entry():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n'
                                '\t(footprint "Lib:R" (property "Reference" "R2"))\n')
        localize(proj, [r for r in rows(proj, "footprint") if r["ref"] == "R1"])
        localize(proj, [r for r in rows(proj, "footprint") if r["ref"] == "R2"])
        libs = sexpr.parse(read(os.path.join(proj, "fp-lib-table"))).find_all("lib")
        names = [l.find("name").children[1].value for l in libs]
        assert sorted(names) == ["Lib", "Lib_local"], names


def test_entry_is_inserted_into_an_empty_one_line_table():
    with tmp() as root:
        lib = os.path.join(root, "ext", "Lib.pretty")
        write(os.path.join(lib, "R.kicad_mod"), '(footprint "R")\n')
        cfg = os.path.join(root, "cfg")
        write(os.path.join(cfg, "10.0", "fp-lib-table"),
              table("fp", ("Lib", "KiCad", fwd(lib))))
        proj = os.path.join(root, "proj")
        write(os.path.join(proj, "fp-lib-table"), "(fp_lib_table)\n")
        write(os.path.join(proj, "b.kicad_pcb"),
              '(kicad_pcb (footprint "Lib:R" (property "Reference" "R1")))\n')
        with env(KICAD_CONFIG_HOME=cfg):
            localize(proj, rows(proj, "footprint"))
            assert portability.resolve_lib_uri(proj, "fp-lib-table", "Lib_local") == \
                "${KIPRJMOD}/libs/Lib_local.pretty"
            assert rows(proj, "footprint")[0]["status"] == "local"


def test_new_project_table_is_created_and_reverted():
    with tmp() as root:
        lib = os.path.join(root, "ext", "Lib.pretty")
        write(os.path.join(lib, "R.kicad_mod"), '(footprint "R")\n')
        cfg = os.path.join(root, "cfg")
        write(os.path.join(cfg, "10.0", "fp-lib-table"),
              table("fp", ("Lib", "KiCad", fwd(lib))))
        proj = os.path.join(root, "proj")
        write(os.path.join(proj, "b.kicad_pcb"),
              '(kicad_pcb (footprint "Lib:R" (property "Reference" "R1")))\n')
        with env(KICAD_CONFIG_HOME=cfg):
            localize(proj, rows(proj, "footprint"))
            t = os.path.join(proj, "fp-lib-table")
            assert os.path.isfile(t)
            sexpr.parse(read(t))
            res = portability.revert(proj)
            assert res["errors"] == [], res
            assert not os.path.exists(t)
            assert os.listdir(proj) == ["b.kicad_pcb"], os.listdir(proj)


def test_parse_lib_table_handles_missing_file_and_fields():
    with tmp() as root:
        assert portability.parse_lib_table(os.path.join(root, "nope")) == {}
        t = os.path.join(root, "fp-lib-table")
        write(t, '(fp_lib_table (lib (name "A")(uri "/a")) (lib (name "NoUri")) '
                 '(lib (uri "/nameless")) (lib (name "B")(type "KiCad")(uri "/b")))\n')
        assert portability.parse_lib_table(t) == {"A": "/a", "B": "/b"}


# --- footprints ---------------------------------------------------------------------

def test_only_selected_footprints_move():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n'
                                '\t(footprint "Lib:R" (property "Reference" "R2"))\n'
                                '\t(footprint "Lib:C" (property "Reference" "C1"))\n',
                          libs=(("Lib", ("R", "C")),))
        localize(proj, [r for r in rows(proj, "footprint") if r["ref"] in ("R1", "C1")])
        got = {r["ref"]: r["name"] for r in rows(proj, "footprint")}
        assert got == {"R1": "Lib_local:R", "R2": "Lib:R", "C1": "Lib_local:C"}, got
        assert sorted(os.listdir(os.path.join(proj, "libs", "Lib_local.pretty"))) == \
            ["C.kicad_mod", "R.kicad_mod"]


def test_footprint_without_nickname_is_local_and_unknown_nickname_missing():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "R_bare" (property "Reference" "R1"))\n'
                                '\t(footprint "Nope:R" (property "Reference" "R2"))\n')
        got = {r["ref"]: (r["status"], r["wanted"]) for r in rows(proj, "footprint")}
        assert got == {"R1": ("local", False), "R2": ("missing", False)}, got


def test_several_libraries_in_one_run():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "A:X" (property "Reference" "U1"))\n'
                                '\t(footprint "B:Y" (property "Reference" "U2"))\n',
                          libs=(("A", ("X",)), ("B", ("Y",))))
        localize(proj, rows(proj, "footprint"))
        assert {r["name"] for r in rows(proj, "footprint")} == {"A_local:X", "B_local:Y"}
        assert all(r["status"] == "local" for r in rows(proj, "footprint"))


def test_project_path_with_spaces_and_unicode():
    with tmp() as base:
        root = os.path.join(base, "my projects", "ünï 中")
        with env():
            proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n')
            localize(proj, rows(proj, "footprint"))
            assert rows(proj, "footprint")[0]["status"] == "local"
            assert portability.revert(proj)["errors"] == []
            assert rows(proj, "footprint")[0]["name"] == "Lib:R"


def test_localize_then_revert_is_byte_exact():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n')
        before = {f: read(os.path.join(proj, f)) for f in os.listdir(proj)}
        localize(proj, rows(proj, "footprint"))
        assert portability.has_backup(proj)
        portability.revert(proj)
        assert {f: read(os.path.join(proj, f)) for f in os.listdir(proj)} == before
        assert not portability.has_backup(proj)


def _read_only(path):
    os.chmod(path, stat.S_IREAD)


def _writable(path):
    return bool(os.stat(path).st_mode & stat.S_IWRITE)


def test_copies_of_read_only_stock_files_are_writable():
    # KiCad installs its libraries read-only. A copied read-only flag made
    # KiCad open the project library read-only and Revert fail on Windows.
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n')
        src = os.path.join(root, "ext", "Lib.pretty", "R.kicad_mod")
        _read_only(src)
        try:
            localize(proj, rows(proj, "footprint"))
            assert _writable(os.path.join(proj, "libs", "Lib_local.pretty", "R.kicad_mod"))
            res = portability.revert(proj)
            assert res["errors"] == [], res
            assert not os.path.exists(os.path.join(proj, "libs"))
        finally:
            os.chmod(src, stat.S_IREAD | stat.S_IWRITE)


def test_read_only_symbol_and_model_copies_are_writable():
    with tmp() as root, env():
        proj = _sym_project(root)
        _read_only(os.path.join(root, "ext", "My.kicad_sym"))
        step = os.path.join(root, "ext", "R.3dshapes", "R.step")
        write(step, "x")
        _read_only(step)
        write(os.path.join(proj, "b.kicad_pcb"),
              '(kicad_pcb (footprint "R" (property "Reference" "R1") (model "{}")))\n'
              .format(fwd(step)))
        try:
            localize(proj, [r for r in rows(proj) if r["wanted"]])
            assert _writable(os.path.join(proj, "libs", "My_local.kicad_sym"))
            assert _writable(os.path.join(proj, "packages3D", "R.3dshapes", "R.step"))
            assert portability.revert(proj)["errors"] == []
        finally:
            for p in (os.path.join(root, "ext", "My.kicad_sym"), step):
                os.chmod(p, stat.S_IREAD | stat.S_IWRITE)


def test_revert_removes_read_only_copies_from_older_versions():
    with tmp() as root, env():
        proj = fp_project(root, '\t(footprint "Lib:R" (property "Reference" "R1"))\n')
        localize(proj, rows(proj, "footprint"))
        _read_only(os.path.join(proj, "libs", "Lib_local.pretty", "R.kicad_mod"))
        res = portability.revert(proj)
        assert res["errors"] == [], res
        assert not os.path.exists(os.path.join(proj, "libs"))


def test_revert_without_backup_reports_it():
    with tmp() as root:
        res = portability.revert(root)
        assert res["restored"] == 0 and res["errors"], res


# --- symbols -------------------------------------------------------------------------

SCH = ('(kicad_sch\n'
       '\t(lib_symbols\n'
       '\t\t(symbol "My:R" (symbol "R_0_1" (rectangle)) (symbol "R_1_1"))\n'
       '\t\t(symbol "My:R2" (extends "R"))\n'
       '\t\t(symbol "Other:X" (symbol "X_0_1"))\n'
       '\t)\n'
       '\t(symbol (lib_id "My:R") (property "Reference" "R1"))\n'
       '\t(symbol (lib_id "My:R2") (property "Reference" "R2"))\n'
       '\t(symbol (lib_id "Other:X") (property "Reference" "U1"))\n'
       ')\n')


def _sym_project(root):
    proj = os.path.join(root, "proj")
    my = os.path.join(root, "ext", "My.kicad_sym")
    other = os.path.join(root, "ext", "Other.kicad_sym")
    write(my, '(kicad_symbol_lib (symbol "R") (symbol "R2" (extends "R")))\n')
    write(other, '(kicad_symbol_lib (symbol "X"))\n')
    write(os.path.join(proj, "sym-lib-table"),
          table("sym", ("My", "KiCad", fwd(my)), ("Other", "KiCad", fwd(other))))
    write(os.path.join(proj, "a.kicad_sch"), SCH)
    return proj


def test_symbol_remap_touches_only_the_selected_library():
    with tmp() as root, env():
        proj = _sym_project(root)
        localize(proj, [r for r in rows(proj, "symbol") if r["ref"] == "R1"])
        want = (SCH.replace('"My:R"', '"My_local:R"')
                   .replace('"My:R2"', '"My_local:R2"'))
        assert read(os.path.join(proj, "a.kicad_sch")) == want


def test_symbol_library_is_copied_whole():
    with tmp() as root, env():
        proj = _sym_project(root)
        localize(proj, [r for r in rows(proj, "symbol") if r["ref"] == "R1"])
        copied = read(os.path.join(proj, "libs", "My_local.kicad_sym"))
        assert '(symbol "R2" (extends "R"))' in copied
        got = {r["ref"]: r["status"] for r in rows(proj, "symbol")}
        assert got == {"R1": "local", "R2": "local", "U1": "external"}, got


def test_schematic_without_matching_symbols_is_not_rewritten():
    with tmp() as root, env():
        proj = _sym_project(root)
        other = os.path.join(proj, "b.kicad_sch")
        write(other, '(kicad_sch (symbol (lib_id "Other:X") (property "Reference" "U9")))\n')
        localize(proj, [r for r in rows(proj, "symbol") if r["ref"] == "R1"])
        man = portability._load_manifest(proj)
        assert "b.kicad_sch" not in {e["path"] for e in man["backed_up"]}


# --- 3D models ----------------------------------------------------------------------

def _model_project(root, files, ref_path):
    proj = os.path.join(root, "proj")
    for rel, data in files.items():
        write(os.path.join(root, rel), data)
    write(os.path.join(proj, "b.kicad_pcb"),
          '(kicad_pcb (footprint "R" (property "Reference" "R1") (model "{}")))\n'
          .format(fwd(os.path.join(root, ref_path))))
    return proj


def test_twins_are_renamed_together():
    with tmp() as root, env():
        proj = _model_project(root, {"ext/Parts.3dshapes/R.step": "step",
                                     "ext/Parts.3dshapes/R.wrl": "new wrl"},
                              "ext/Parts.3dshapes/R.step")
        dest = os.path.join(proj, "packages3D", "Parts.3dshapes")
        write(os.path.join(dest, "R.step"), "step")       # identical
        write(os.path.join(dest, "R.wrl"), "user's wrl")  # different
        localize(proj, rows(proj, "model"))
        assert sorted(os.listdir(dest)) == ["R.step", "R.wrl", "R_1.step", "R_1.wrl"]
        assert read(os.path.join(dest, "R.wrl")) == "user's wrl"
        assert "${KIPRJMOD}/packages3D/Parts.3dshapes/R_1.step" in \
            read(os.path.join(proj, "b.kicad_pcb"))


def test_identical_model_already_in_place_is_reused():
    with tmp() as root, env():
        proj = _model_project(root, {"ext/Parts.3dshapes/R.step": "step"},
                              "ext/Parts.3dshapes/R.step")
        dest = os.path.join(proj, "packages3D", "Parts.3dshapes")
        write(os.path.join(dest, "R.step"), "step")
        localize(proj, rows(proj, "model"))
        assert os.listdir(dest) == ["R.step"]
        portability.revert(proj)
        assert os.listdir(dest) == ["R.step"], "revert removed a file it did not create"


def test_uppercase_twin_extension_is_copied():
    with tmp() as root, env():
        proj = _model_project(root, {"ext/Parts.3dshapes/R.step": "step",
                                     "ext/Parts.3dshapes/R.WRL": "wrl"},
                              "ext/Parts.3dshapes/R.step")
        localize(proj, rows(proj, "model"))
        got = sorted(n.lower() for n in
                     os.listdir(os.path.join(proj, "packages3D", "Parts.3dshapes")))
        assert got == ["r.step", "r.wrl"], got


def test_missing_model_is_not_wanted():
    with tmp() as root, env():
        proj = _model_project(root, {}, "ext/Parts.3dshapes/Gone.step")
        r = rows(proj, "model")[0]
        assert (r["status"], r["wanted"]) == ("missing", False)


def test_model_under_the_library_var_is_standard():
    with tmp() as root:
        write(os.path.join(root, "lib3d", "R.3dshapes", "R.step"), "x")
        with env(KICAD10_3DMODEL_DIR=os.path.join(root, "lib3d")):
            assert portability.classify("${KICAD10_3DMODEL_DIR}/R.3dshapes/R.step",
                                        root) == "standard"
            assert portability.classify("${KICAD9_3DMODEL_DIR}/R.3dshapes/R.step",
                                        root) == "standard"


if __name__ == "__main__":
    run_all(globals())
