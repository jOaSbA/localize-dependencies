# Path and library-table resolution, the way KiCad itself resolves them.
# Issue #2: on a KiCad 10 flatpak every stock footprint showed as MISSING and
# custom libraries were not found, because the plugin
#   - did not map an unset ${KICAD9_FOOTPRINT_DIR} to KICAD10_FOOTPRINT_DIR,
#   - did not know the flatpak library folder /app/extensions/Library,
#   - read ~/.config/kicad instead of KICAD_CONFIG_HOME / XDG_CONFIG_HOME,
#   - did not follow KiCad 10's nested (type "Table") library entries, and
#     fell through to an older KiCad version's global table instead.
#
# Run: python tests/test_paths.py   (or pytest)
#
# License: GPL-3.0-or-later

import glob
import os
import sys

from _util import env, fwd, portability, run_all, table, tmp, write


def _resolve(raw, project_dir="."):
    return portability.make_resolver(project_dir)(raw)


# --- fix A: version-agnostic ${KICADn_*} -------------------------------------

def test_unset_old_version_var_uses_current_version():
    with env(KICAD10_FOOTPRINT_DIR="/lib10/footprints"):
        assert _resolve("${KICAD9_FOOTPRINT_DIR}/R.pretty") == \
            os.path.normpath("/lib10/footprints/R.pretty")


def test_newest_set_version_wins():
    with env(KICAD8_FOOTPRINT_DIR="/lib8", KICAD10_FOOTPRINT_DIR="/lib10",
              KICAD7_FOOTPRINT_DIR="/lib7"):
        assert _resolve("${KICAD9_FOOTPRINT_DIR}/R.pretty") == \
            os.path.normpath("/lib10/R.pretty")


def test_version_numbers_compare_as_numbers():
    # "9" sorts above "10" as a string.
    with env(KICAD9_SYMBOL_DIR="/lib9", KICAD10_SYMBOL_DIR="/lib10"):
        assert _resolve("${KICAD6_SYMBOL_DIR}/D.kicad_sym") == \
            os.path.normpath("/lib10/D.kicad_sym")


def test_exact_variable_beats_versioned_lookup():
    with env(KICAD9_FOOTPRINT_DIR="/lib9", KICAD10_FOOTPRINT_DIR="/lib10"):
        assert _resolve("${KICAD9_FOOTPRINT_DIR}/R.pretty") == \
            os.path.normpath("/lib9/R.pretty")


def test_versioned_lookup_keeps_the_kind():
    # A symbol dir must never stand in for a footprint dir.
    with env(KICAD10_SYMBOL_DIR="/sym10"):
        assert _resolve("${KICAD9_FOOTPRINT_DIR}/R.pretty") == \
            os.path.normpath("${KICAD9_FOOTPRINT_DIR}/R.pretty")


def test_versioned_lookup_skips_empty_values():
    with env(KICAD10_FOOTPRINT_DIR="", KICAD8_FOOTPRINT_DIR="/lib8"):
        assert _resolve("${KICAD9_FOOTPRINT_DIR}/R.pretty") == \
            os.path.normpath("/lib8/R.pretty")


def test_versioned_lookup_covers_3rd_party():
    with env(KICAD10_3RD_PARTY="/3rd10"):
        assert _resolve("${KICAD9_3RD_PARTY}/x.pretty") == \
            os.path.normpath("/3rd10/x.pretty")


def test_unversioned_variable_is_not_guessed():
    with env(KICAD10_3RD_PARTY="/3rd10"):
        assert _resolve("${KICAD_3RD_PARTY}/x.pretty") == \
            os.path.normpath("${KICAD_3RD_PARTY}/x.pretty")


def test_paren_syntax_gets_versioned_lookup_too():
    with env(KICAD10_3DMODEL_DIR="/m10"):
        assert _resolve("$(KICAD9_3DMODEL_DIR)/R.step") == \
            os.path.normpath("/m10/R.step")


def test_kiprjmod_still_wins():
    with env(KICAD10_FOOTPRINT_DIR="/lib10"):
        assert _resolve("${KIPRJMOD}/libs/x.pretty", "/proj") == \
            os.path.normpath("/proj/libs/x.pretty")


# --- fix B: install-folder fallback, flatpak and per-user Windows ---------------

def test_flatpak_library_folder_is_a_known_install():
    assert "/app/extensions/Library" in portability._INSTALL_GLOBS


def test_install_folder_fallback_for_unset_vars():
    with tmp() as root, env():
        lib = os.path.join(root, "app", "extensions", "Library")
        for sub in ("footprints", "symbols", "3dmodels"):
            os.makedirs(os.path.join(lib, sub))
        portability._INSTALL_GLOBS = (lib,)
        assert _resolve("${KICAD9_FOOTPRINT_DIR}/R.pretty") == \
            os.path.join(lib, "footprints", "R.pretty")
        assert _resolve("${KICAD9_SYMBOL_DIR}/D.kicad_sym") == \
            os.path.join(lib, "symbols", "D.kicad_sym")
        assert _resolve("${KICAD9_3DMODEL_DIR}/R.step") == \
            os.path.join(lib, "3dmodels", "R.step")


def test_env_var_beats_install_folder():
    with tmp() as root, env(KICAD10_FOOTPRINT_DIR="/lib10"):
        os.makedirs(os.path.join(root, "footprints"))
        portability._INSTALL_GLOBS = (root,)
        assert _resolve("${KICAD9_FOOTPRINT_DIR}/R.pretty") == \
            os.path.normpath("/lib10/R.pretty")


def test_windows_per_user_install_is_a_known_install():
    if os.name != "nt":
        return
    want = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "KiCad", "*",
                        "share", "kicad")
    assert want in portability._INSTALL_GLOBS, portability._INSTALL_GLOBS


# --- fix C: where KiCad keeps its settings ------------------------------------

def test_kicad_config_home_wins():
    with tmp() as root, env(KICAD_CONFIG_HOME=root, XDG_CONFIG_HOME="/elsewhere"):
        for v in ("9.0", "10.0"):
            os.makedirs(os.path.join(root, v))
        assert [os.path.basename(d) for d in portability._config_dirs()] == \
            ["10.0", "9.0"]


def test_linux_honors_xdg_config_home():
    # Flatpak sets XDG_CONFIG_HOME to ~/.var/app/org.kicad.KiCad/config.
    if os.name == "nt" or sys.platform == "darwin":
        return
    with tmp() as root, env(XDG_CONFIG_HOME=root):
        del os.environ["KICAD_CONFIG_HOME"]
        os.makedirs(os.path.join(root, "kicad", "10.0"))
        assert portability._config_dirs() == [os.path.join(root, "kicad", "10.0")]


def test_linux_default_is_dot_config():
    if os.name == "nt" or sys.platform == "darwin":
        return
    with env():
        del os.environ["KICAD_CONFIG_HOME"]
        assert portability._config_base() == os.path.expanduser("~/.config/kicad")


def test_macos_uses_library_preferences():
    if sys.platform != "darwin":
        return
    with env():
        del os.environ["KICAD_CONFIG_HOME"]
        assert portability._config_base() == \
            os.path.expanduser("~/Library/Preferences/kicad")


def test_windows_uses_appdata():
    if os.name != "nt":
        return
    with env():
        del os.environ["KICAD_CONFIG_HOME"]
        assert portability._config_base() == \
            os.path.join(os.environ["APPDATA"], "kicad")


def test_missing_config_dir_is_empty_not_an_error():
    with tmp() as root, env(KICAD_CONFIG_HOME=os.path.join(root, "nope")):
        assert portability._config_dirs() == []


# --- fix D: KiCad 10 nested tables, newest version only -----------------------

def _kicad10_layout(root):
    """A KiCad 10 setup like the one in issue #2. Returns (cfg, share, custom).

    share/template/fp-lib-table   stock table, ${KICAD10_FOOTPRINT_DIR} uris
    cfg/10.0/fp-lib-table          a Table entry for the stock table + MyParts
    cfg/9.0/fp-lib-table           stale KiCad 9 table: stock libs with
                                   ${KICAD9_*} uris and a library 10.0 dropped
    """
    cfg = os.path.join(root, "cfg")
    share = os.path.join(root, "share")
    custom = os.path.join(root, "custom")
    write(os.path.join(share, "footprints", "Capacitor_THT.pretty", "C.kicad_mod"),
           '(footprint "C" (descr "v10"))\n')
    write(os.path.join(share, "template", "fp-lib-table"), table(
        "fp", ("Capacitor_THT", "KiCad", "${KICAD10_FOOTPRINT_DIR}/Capacitor_THT.pretty")))
    write(os.path.join(custom, "MyParts.pretty", "X.kicad_mod"), '(footprint "X")\n')
    write(os.path.join(cfg, "10.0", "fp-lib-table"), table(
        "fp",
        ("KiCad", "Table", fwd(os.path.join(share, "template", "fp-lib-table"))),
        ("MyParts", "KiCad", "${KICAD_3RD_PARTY}/MyParts.pretty")))
    write(os.path.join(cfg, "9.0", "fp-lib-table"), table(
        "fp",
        ("Capacitor_THT", "KiCad", "${KICAD9_FOOTPRINT_DIR}/Capacitor_THT.pretty"),
        ("OldOnly", "KiCad", "${KICAD9_FOOTPRINT_DIR}/OldOnly.pretty")))
    return cfg, share, custom


def test_nested_table_entry_is_followed():
    with tmp() as root:
        cfg, share, custom = _kicad10_layout(root)
        with env(KICAD_CONFIG_HOME=cfg):
            assert portability.resolve_lib_uri(root, "fp-lib-table", "Capacitor_THT") \
                == "${KICAD10_FOOTPRINT_DIR}/Capacitor_THT.pretty"
            assert portability.resolve_lib_uri(root, "fp-lib-table", "MyParts") \
                == "${KICAD_3RD_PARTY}/MyParts.pretty"


def test_older_version_tables_are_ignored():
    with tmp() as root:
        cfg, _, _ = _kicad10_layout(root)
        with env(KICAD_CONFIG_HOME=cfg):
            assert portability.resolve_lib_uri(root, "fp-lib-table", "OldOnly") is None


def test_newest_version_without_a_table_falls_back_to_the_next():
    # KiCad 11 config dir created, tables not migrated yet.
    with tmp() as root:
        cfg, _, _ = _kicad10_layout(root)
        os.makedirs(os.path.join(cfg, "11.0"))
        with env(KICAD_CONFIG_HOME=cfg):
            assert portability.resolve_lib_uri(root, "fp-lib-table", "MyParts") \
                == "${KICAD_3RD_PARTY}/MyParts.pretty"


def test_project_table_beats_globaltable():
    with tmp() as root:
        cfg, _, _ = _kicad10_layout(root)
        proj = os.path.join(root, "proj")
        write(os.path.join(proj, "fp-lib-table"),
               table("fp", ("MyParts", "KiCad", "${KIPRJMOD}/MyParts.pretty")))
        with env(KICAD_CONFIG_HOME=cfg):
            assert portability.resolve_lib_uri(proj, "fp-lib-table", "MyParts") \
                == "${KIPRJMOD}/MyParts.pretty"


def test_direct_entry_beats_nested_entry():
    with tmp() as root:
        cfg, share, _ = _kicad10_layout(root)
        write(os.path.join(cfg, "10.0", "fp-lib-table"), table(
            "fp",
            ("KiCad", "Table", fwd(os.path.join(share, "template", "fp-lib-table"))),
            ("Capacitor_THT", "KiCad", "/mine/Capacitor_THT.pretty")))
        with env(KICAD_CONFIG_HOME=cfg):
            assert portability.resolve_lib_uri(root, "fp-lib-table", "Capacitor_THT") \
                == "/mine/Capacitor_THT.pretty"


def test_nested_table_uri_with_variable():
    with tmp() as root:
        cfg, share, _ = _kicad10_layout(root)
        write(os.path.join(cfg, "10.0", "fp-lib-table"), table(
            "fp", ("KiCad", "Table", "${KICAD10_TEMPLATE_DIR}/fp-lib-table")))
        with env(KICAD_CONFIG_HOME=cfg,
                  KICAD10_TEMPLATE_DIR=os.path.join(share, "template")):
            assert portability.resolve_lib_uri(root, "fp-lib-table", "Capacitor_THT") \
                == "${KICAD10_FOOTPRINT_DIR}/Capacitor_THT.pretty"


def test_dead_nested_table_path_falls_back_to_current_install():
    # KiCad reinstalled elsewhere: the Table entry still names the old folder.
    with tmp() as root:
        cfg, share, _ = _kicad10_layout(root)
        write(os.path.join(cfg, "10.0", "fp-lib-table"), table(
            "fp", ("KiCad", "Table", "C:/Program Files/KiCad/10.0/share/kicad/"
                                     "template/fp-lib-table")))
        with env(KICAD_CONFIG_HOME=cfg):
            portability._INSTALL_GLOBS = (share,)
            assert portability.resolve_lib_uri(root, "fp-lib-table", "Capacitor_THT") \
                == "${KICAD10_FOOTPRINT_DIR}/Capacitor_THT.pretty"


def test_table_cycle_terminates():
    with tmp() as root:
        cfg = os.path.join(root, "cfg")
        a = os.path.join(root, "a-lib-table")
        b = os.path.join(root, "b-lib-table")
        write(a, table("fp", ("B", "Table", fwd(b))))
        write(b, table("fp", ("A", "Table", fwd(a))))
        write(os.path.join(cfg, "10.0", "fp-lib-table"),
               table("fp", ("A", "Table", fwd(a))))
        with env(KICAD_CONFIG_HOME=cfg):
            assert portability.resolve_lib_uri(root, "fp-lib-table", "Nope") is None


def test_table_named_like_the_nickname_is_not_a_library():
    with tmp() as root:
        cfg, _, _ = _kicad10_layout(root)
        with env(KICAD_CONFIG_HOME=cfg):
            assert portability.resolve_lib_uri(root, "fp-lib-table", "KiCad") is None


def test_edited_table_is_read_again():
    with tmp() as root:
        proj = os.path.join(root, "proj")
        path = os.path.join(proj, "fp-lib-table")
        with env(KICAD_CONFIG_HOME=os.path.join(root, "none")):
            write(path, table("fp", ("L", "KiCad", "/one/L.pretty")))
            assert portability.resolve_lib_uri(proj, "fp-lib-table", "L") == "/one/L.pretty"
            write(path, table("fp", ("L", "KiCad", "/second/L.pretty")))
            assert portability.resolve_lib_uri(proj, "fp-lib-table", "L") == \
                "/second/L.pretty"


def test_sym_tables_follow_the_same_rules():
    with tmp() as root:
        cfg = os.path.join(root, "cfg")
        tmpl = os.path.join(root, "share", "template", "sym-lib-table")
        write(tmpl, table("sym", ("Device", "KiCad", "${KICAD10_SYMBOL_DIR}/Device.kicad_sym")))
        write(os.path.join(cfg, "10.0", "sym-lib-table"),
               table("sym", ("KiCad", "Table", fwd(tmpl))))
        with env(KICAD_CONFIG_HOME=cfg):
            assert portability.resolve_lib_uri(root, "sym-lib-table", "Device") == \
                "${KICAD10_SYMBOL_DIR}/Device.kicad_sym"


# --- the issue #2 setup, end to end -----------------------------------------------

def _issue2_project(root):
    cfg, share, custom = _kicad10_layout(root)
    proj = os.path.join(root, "proj")
    write(os.path.join(proj, "board.kicad_pcb"),
           "(kicad_pcb\n"
           '\t(footprint "Capacitor_THT:C" (property "Reference" "C1"))\n'
           '\t(footprint "MyParts:X" (property "Reference" "U1"))\n'
           '\t(footprint "OldOnly:Y" (property "Reference" "Q1"))\n'
           ")\n")
    kicad_env = dict(KICAD_CONFIG_HOME=cfg,
                     KICAD10_FOOTPRINT_DIR=os.path.join(share, "footprints"),
                     KICAD_3RD_PARTY=custom)
    return proj, kicad_env


def test_issue2_scan_finds_stock_and_custom_parts():
    with tmp() as root:
        proj, kicad_env = _issue2_project(root)
        with env(**kicad_env):
            rows = {r["ref"]: r for r in portability.scan_items(proj)
                    if r["type"] == "footprint"}
    assert rows["C1"]["status"] == "standard", rows["C1"]
    assert rows["U1"]["status"] == "external", rows["U1"]
    assert rows["Q1"]["status"] == "missing", rows["Q1"]
    assert rows["U1"]["wanted"] and not rows["C1"]["wanted"]


def test_issue2_localize_copies_the_kicad10_footprint():
    with tmp() as root:
        proj, kicad_env = _issue2_project(root)
        with env(**kicad_env):
            rows = portability.scan_items(proj, include_standard=True)
            s = portability.Session(proj)
            s.save_manifest(portability.apply_selection(s, rows))
            copied = glob.glob(os.path.join(proj, "**", "C.kicad_mod"), recursive=True)
            assert len(copied) == 1, copied
            with open(copied[0], encoding="utf-8") as fh:
                assert "v10" in fh.read()
            assert glob.glob(os.path.join(proj, "**", "X.kicad_mod"), recursive=True)
            rows = {r["ref"]: r for r in portability.scan_items(proj)
                    if r["type"] == "footprint"}
            assert rows["C1"]["status"] == "local", rows["C1"]
            assert rows["U1"]["status"] == "local", rows["U1"]
            portability.revert(proj)
            assert not glob.glob(os.path.join(proj, "**", "*.kicad_mod"), recursive=True)


if __name__ == "__main__":
    run_all(globals())
