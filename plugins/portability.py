# Project portability engine (file-level, pure Python, no KiCad or wx imports).
#
# Localizes a KiCad project's external dependencies into project-local libraries
# so the project is self-contained and portable: 3D models, footprints, and
# symbols. Operates directly on the saved project files via the generic S-expr
# parser (sexpr.py), editing only the exact byte spans it targets.
#
# SAFETY: every run backs up each file before modifying it and records a
# manifest (.portability/manifest.json). revert() restores those files
# byte-for-byte and removes anything the run created. Nothing is destructive
# without a recorded undo.
#
# License: GPL-3.0-or-later

import glob
import json
import os
import re
import shutil

import sexpr

# --- layout ------------------------------------------------------------------

STATE_DIRNAME = ".portability"           # holds backups + manifest
MANIFEST_NAME = "manifest.json"
LOCAL_3D_DIRNAME = "packages3D"          # local 3D models (matches KiCad convention)
LOCAL_FP_SUFFIX = ".pretty"
TWIN_EXTS = (".step", ".stp", ".wrl")

_MODEL_DIR_VARS = (
    "KISYS3DMOD", "KICAD6_3DMODEL_DIR", "KICAD7_3DMODEL_DIR",
    "KICAD8_3DMODEL_DIR", "KICAD9_3DMODEL_DIR", "KICAD10_3DMODEL_DIR",
)
_INSTALL_GLOBS = (
    r"C:\Program Files\KiCad\*\share\kicad",
    r"C:\Program Files (x86)\KiCad\*\share\kicad",
    "/usr/share/kicad",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
)


# --- path resolution ---------------------------------------------------------

def _install_share():
    hits = []
    for pat in _INSTALL_GLOBS:
        hits.extend(p for p in glob.glob(pat) if os.path.isdir(p))
    hits.sort(reverse=True)
    return hits[0] if hits else None


def make_resolver(project_dir):
    """Return resolve(raw)->absolute path, expanding ${KIPRJMOD}, KiCad library
    dir vars, and any environment variables. Falls back to the KiCad install
    share dir for unset ${KICAD*_{3DMODEL,FOOTPRINT,SYMBOL}_DIR} (common: the
    plugin runs in a separate process without those set)."""
    share = _install_share()
    env_model = {v: os.environ[v] for v in _MODEL_DIR_VARS
                 if os.environ.get(v) and os.path.isdir(os.environ[v])}
    fallbacks = {}   # var-suffix -> dir
    if share:
        for sub, suffix in (("3dmodels", "_3DMODEL_DIR"),
                            ("footprints", "_FOOTPRINT_DIR"),
                            ("symbols", "_SYMBOL_DIR")):
            d = os.path.join(share, sub)
            if os.path.isdir(d):
                fallbacks[suffix] = d

    def resolve(raw):
        if not raw:
            return raw
        s = re.sub(r"\$\(([^)]+)\)", r"${\1}", raw)   # $(VAR) -> ${VAR}

        def repl(m):
            name = m.group(1)
            if name == "KIPRJMOD":
                return project_dir
            if name in env_model:
                return env_model[name]
            val = os.environ.get(name)
            if val:
                return val
            for suffix, d in fallbacks.items():
                if name.endswith(suffix):
                    return d
            return m.group(0)

        s = re.sub(r"\$\{([^}]+)\}", repl, s)
        return os.path.normpath(s)

    return resolve


# --- library tables ----------------------------------------------------------

def parse_lib_table(path):
    """nickname -> uri from a sym-lib-table / fp-lib-table file (or {} if none)."""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        root = sexpr.parse(fh.read())
    for lib in root.find_all("lib"):
        name = lib.find("name")
        uri = lib.find("uri")
        if name and uri and len(name.children) > 1 and len(uri.children) > 1:
            out[name.children[1].value] = uri.children[1].value
    return out


def _config_dirs():
    base = os.path.expandvars(r"%APPDATA%\kicad") if os.name == "nt" else \
        os.path.expanduser("~/.config/kicad")
    return sorted(glob.glob(os.path.join(base, "*")), reverse=True) \
        if os.path.isdir(base) else []


def resolve_lib_uri(project_dir, table_name, nickname):
    """Resolve a library nickname to its uri, checking the project table first,
    then the newest global table."""
    proj = parse_lib_table(os.path.join(project_dir, table_name))
    if nickname in proj:
        return proj[nickname]
    for d in _config_dirs():
        g = parse_lib_table(os.path.join(d, table_name))
        if nickname in g:
            return g[nickname]
    return None


def _table_header(kind):
    return "fp_lib_table" if kind == "fp" else "sym_lib_table"


def _lib_entry(nickname, uri):
    return ('\t(lib (name "{}")(type "KiCad")(uri "{}")(options "")'
            '(descr "Localized"))').format(nickname, uri)


def add_lib_entries(session, kind, entries):
    """Add {nickname: uri} entries to the project's fp/sym lib table, creating
    the table file if needed. Skips nicknames already present."""
    if not entries:
        return
    table_name = "fp-lib-table" if kind == "fp" else "sym-lib-table"
    path = os.path.join(session.project_dir, table_name)
    existing = parse_lib_table(path)
    new = {n: u for n, u in entries.items() if n not in existing}
    if not new:
        return
    lines = "\n".join(_lib_entry(n, u) for n, u in sorted(new.items()))

    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        root = sexpr.parse(text)
        close = root.end - 1                      # offset of final ')'
        new_text = text[:close] + lines + "\n" + text[close:]
        write_validated(session, path, new_text)
    else:
        new_text = "({}\n\t(version 7)\n{}\n)\n".format(_table_header(kind), lines)
        write_validated(session, path, new_text, is_new=True)


def classify(raw, project_dir):
    """external / standard / local / missing for a raw model path."""
    if "${KIPRJMOD}" in raw:
        return "local"
    resolved = make_resolver(project_dir)(raw)
    is_std = bool(re.search(r"\$\{KICAD\d*_3DMODEL_DIR\}", raw)) or (
        _install_share() and _install_share().replace("\\", "/").lower()
        in resolved.replace("\\", "/").lower())
    if not os.path.isfile(resolved):
        return "missing"
    return "standard" if is_std else "external"


# --- backup / manifest / revert ---------------------------------------------

class Session:
    """Tracks a localize run for safe, exact revert."""

    def __init__(self, project_dir):
        self.project_dir = os.path.abspath(project_dir)
        self.state_dir = os.path.join(self.project_dir, STATE_DIRNAME)
        self._backed = {}      # abs original path -> backup rel path
        self._created_files = []
        self._created_dirs = []

    def _rel(self, path):
        return os.path.relpath(path, self.project_dir).replace(os.sep, "/")

    def backup(self, path):
        """Copy `path` into the backup store once, before it is modified."""
        path = os.path.abspath(path)
        if path in self._backed or not os.path.isfile(path):
            return
        rel = self._rel(path)
        dest = os.path.join(self.state_dir, "backup", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        self._backed[path] = os.path.join("backup", rel).replace(os.sep, "/")

    def mkdir(self, path):
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            self._created_dirs.append(self._rel(path))

    def wrote_new_file(self, path):
        self._created_files.append(self._rel(os.path.abspath(path)))

    def save_manifest(self, summary):
        os.makedirs(self.state_dir, exist_ok=True)
        manifest = {
            "backed_up": [{"path": self._rel(p), "backup": b}
                          for p, b in self._backed.items()],
            "created_files": self._created_files,
            "created_dirs": self._created_dirs,
            "summary": summary,
        }
        with open(os.path.join(self.state_dir, MANIFEST_NAME), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)


def has_backup(project_dir):
    return os.path.isfile(os.path.join(project_dir, STATE_DIRNAME, MANIFEST_NAME))


def revert(project_dir):
    """Restore backed-up files byte-for-byte and remove created files/dirs.
    Returns a summary dict."""
    project_dir = os.path.abspath(project_dir)
    state_dir = os.path.join(project_dir, STATE_DIRNAME)
    mpath = os.path.join(state_dir, MANIFEST_NAME)
    if not os.path.isfile(mpath):
        return {"restored": 0, "removed": 0, "errors": ["No portability backup found."]}
    with open(mpath, "r", encoding="utf-8") as fh:
        man = json.load(fh)

    restored = removed = 0
    errors = []

    for entry in man.get("backed_up", []):
        src = os.path.join(state_dir, entry["backup"])
        dst = os.path.join(project_dir, entry["path"])
        try:
            shutil.copy2(src, dst)
            restored += 1
        except OSError as exc:
            errors.append("restore {}: {}".format(entry["path"], exc))

    for rel in man.get("created_files", []):
        p = os.path.join(project_dir, rel)
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
        except OSError as exc:
            errors.append("remove {}: {}".format(rel, exc))

    # deepest dirs first so they are empty when removed
    for rel in sorted(man.get("created_dirs", []), key=len, reverse=True):
        p = os.path.join(project_dir, rel)
        try:
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        except OSError:
            pass

    # finally drop the state dir if the manifest restore fully succeeded
    if not errors:
        shutil.rmtree(state_dir, ignore_errors=True)

    return {"restored": restored, "removed": removed, "errors": errors}


# --- validation --------------------------------------------------------------

def write_validated(session, path, new_text, is_new=False):
    """Write new_text to path, but only after confirming it re-parses. Backs up
    an existing file first. Raises on parse failure (leaving the original)."""
    sexpr.parse(new_text)  # will raise SExprError if we produced garbage
    if is_new:
        session.wrote_new_file(path)
    else:
        session.backup(path)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)


def _prop(node, name):
    """Value of a (property "name" "value" ...) child, or None."""
    for c in node.children:
        if (c.kind == "list" and c.head() == "property" and len(c.children) >= 3
                and c.children[1].value == name):
            return c.children[2].value
    return None


def _symbol_instances(root):
    """Top-level schematic symbol instances (the ones carrying a lib_id).
    Excludes the definitions cached under (lib_symbols ...)."""
    return [s for s in root.find_all("symbol") if s.find("lib_id")]


def scan_items(project_dir, include_standard=False):
    """One row per symbol, footprint, and 3D model, each with a stable selection
    key. Read-only. A row is a dict: type, ref, name, source, status, key, wanted.

    Keys encode document order so apply() can find the same node again:
      symbol    ('symbol', sch_rel, instance_index)
      footprint ('footprint', pcb_rel, footprint_index)
      model     ('model', pcb_rel, footprint_index, model_index)
    """
    rows = []

    for sch in sorted(glob.glob(os.path.join(project_dir, "*.kicad_sch"))):
        rel = os.path.relpath(sch, project_dir).replace(os.sep, "/")
        root = sexpr.parse(open(sch, encoding="utf-8").read())
        for i, sym in enumerate(_symbol_instances(root)):
            lid = sym.find("lib_id")
            if not (lid and lid.children[1:]):
                continue
            libid = lid.children[1].value
            nick = libid.split(":", 1)[0] if ":" in libid else ""
            uri = resolve_lib_uri(project_dir, "sym-lib-table", nick) if nick else None
            cat = classify_lib(uri, project_dir) if nick else "local"
            rows.append({"type": "symbol", "ref": _prop(sym, "Reference") or "?",
                         "name": libid, "source": uri or "", "status": cat,
                         "key": ("symbol", rel, i),
                         "wanted": _wanted(cat, include_standard)})

    for pcb in sorted(glob.glob(os.path.join(project_dir, "*.kicad_pcb"))):
        rel = os.path.relpath(pcb, project_dir).replace(os.sep, "/")
        root = sexpr.parse(open(pcb, encoding="utf-8").read())
        for fi, fp in enumerate(root.find_all("footprint")):
            if not (fp.children[1:] and fp.children[1].kind == "atom"):
                continue
            fpid = fp.children[1].value
            ref = _prop(fp, "Reference") or "?"
            nick = fpid.split(":", 1)[0] if ":" in fpid else ""
            uri = resolve_lib_uri(project_dir, "fp-lib-table", nick) if nick else None
            cat = classify_lib(uri, project_dir) if nick else "local"
            rows.append({"type": "footprint", "ref": ref, "name": fpid,
                         "source": uri or "", "status": cat,
                         "key": ("footprint", rel, fi),
                         "wanted": _wanted(cat, include_standard)})
            for mi, model in enumerate(fp.find_all("model")):
                if not (model.children[1:] and model.children[1].kind == "atom"):
                    continue
                raw = model.children[1].value
                mcat = classify(raw, project_dir)
                rows.append({"type": "model", "ref": ref,
                             "name": os.path.basename(raw.replace("\\", "/")),
                             "source": raw, "status": mcat,
                             "key": ("model", rel, fi, mi),
                             "wanted": _wanted(mcat, include_standard)})
    return rows


# --- 3D model localization ---------------------------------------------------

def _dest_for_model(resolved, project_dir):
    """(abs_dest, kiprjmod_relpath) preserving the *.3dshapes subfolder."""
    fname = os.path.basename(resolved)
    subdir = os.path.basename(os.path.dirname(resolved))
    parts = [LOCAL_3D_DIRNAME] + ([subdir] if subdir else []) + [fname]
    abs_dest = os.path.join(project_dir, *parts)
    rel = "/".join(parts)
    return abs_dest, "${KIPRJMOD}/" + rel


def _copy_with_twins(session, resolved, abs_dest):
    dest_dir = os.path.dirname(abs_dest)
    session.mkdir(dest_dir)
    copied = 0
    stem = os.path.splitext(os.path.basename(resolved))[0]
    src_dir = os.path.dirname(resolved)
    todo = [resolved]
    for ext in TWIN_EXTS:
        for e in (ext, ext.upper()):
            t = os.path.join(src_dir, stem + e)
            if os.path.isfile(t) and t != resolved:
                todo.append(t)
    seen = set()
    for src in todo:
        base = os.path.basename(src)
        key = base.lower()
        if key in seen:
            continue
        seen.add(key)
        dst = os.path.join(dest_dir, base)
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            continue
        shutil.copy2(src, dst)
        session.wrote_new_file(dst)
        copied += 1
    return copied


def localize_models(session, rows):
    """Copy the selected 3D models into packages3D/ and remap their (model "...")
    paths. `rows` are the selected scan_items() rows of type 'model'. File-level
    path; the live IPC path lives in the action layer."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)
    keys = {r["key"] for r in rows if r["type"] == "model"}
    if not keys:
        return {"files_copied": 0, "remapped": 0, "unique_sources": 0}

    by_pcb = {}
    for k in keys:
        by_pcb.setdefault(k[1], set()).add(k)

    files_copied = remapped = 0
    copied_sources = set()
    for rel, kset in by_pcb.items():
        pcb = os.path.join(project_dir, rel)
        text = open(pcb, encoding="utf-8").read()
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        for fi, fp in enumerate(root.find_all("footprint")):
            for mi, model in enumerate(fp.find_all("model")):
                if ("model", rel, fi, mi) not in kset or not model.children[1:]:
                    continue
                atom = model.children[1]
                resolved = resolve(atom.value)
                abs_dest, new_raw = _dest_for_model(resolved, project_dir)
                src_key = os.path.normcase(os.path.abspath(resolved))
                if src_key not in copied_sources:
                    files_copied += _copy_with_twins(session, resolved, abs_dest)
                    copied_sources.add(src_key)
                editor.replace_atom(atom, new_raw, force_quote=True)
                remapped += 1
        write_validated(session, pcb, editor.result())

    return {"files_copied": files_copied, "remapped": remapped,
            "unique_sources": len(copied_sources)}


# --- footprint / symbol library localization ---------------------------------

LOCAL_LIB_DIRNAME = "libs"
LOCAL_SUFFIX = "_local"


def classify_lib(uri, project_dir):
    """external / standard / local / missing for a library uri."""
    if not uri:
        return "missing"
    if "${KIPRJMOD}" in uri:
        return "local"
    resolved = make_resolver(project_dir)(uri)
    share = _install_share()
    is_std = bool(re.search(r"\$\{KICAD\d*_(FOOTPRINT|SYMBOL)_DIR\}", uri)) or (
        share and share.replace("\\", "/").lower() in resolved.replace("\\", "/").lower())
    if not os.path.exists(resolved):
        return "missing"
    return "standard" if is_std else "external"


def _wanted(cat, include_standard):
    return cat == "external" or (cat == "standard" and include_standard)


def localize_footprints(session, rows):
    """Copy the selected footprints into ${KIPRJMOD}/libs/<nick>_local.pretty,
    register those libraries in fp-lib-table, and remap the selected footprint
    IDs. Per-item: only the checked footprints move, and both the original and
    the _local nickname stay valid, so unselected footprints are untouched."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)
    keys = {r["key"] for r in rows if r["type"] == "footprint"}
    if not keys:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    by_pcb = {}
    for k in keys:
        by_pcb.setdefault(k[1], set()).add(k)

    entries = {}
    src_dir_cache = {}
    copied = remapped = 0
    for rel, kset in by_pcb.items():
        pcb = os.path.join(project_dir, rel)
        text = open(pcb, encoding="utf-8").read()
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        for fi, fp in enumerate(root.find_all("footprint")):
            if ("footprint", rel, fi) not in kset:
                continue
            atom = fp.children[1]
            if ":" not in atom.value:
                continue
            nick, name = atom.value.split(":", 1)
            if nick not in src_dir_cache:
                src_dir_cache[nick] = resolve(
                    resolve_lib_uri(project_dir, "fp-lib-table", nick) or "")
            local_nick = nick + LOCAL_SUFFIX
            local_dir = os.path.join(project_dir, LOCAL_LIB_DIRNAME,
                                     local_nick + LOCAL_FP_SUFFIX)
            session.mkdir(local_dir)
            src_file = os.path.join(src_dir_cache[nick], name + ".kicad_mod")
            dst_file = os.path.join(local_dir, name + ".kicad_mod")
            if os.path.isfile(src_file) and not os.path.isfile(dst_file):
                shutil.copy2(src_file, dst_file)
                session.wrote_new_file(dst_file)
                copied += 1
            editor.replace_atom(atom, "{}:{}".format(local_nick, name), force_quote=True)
            entries[local_nick] = "${{KIPRJMOD}}/{}/{}{}".format(
                LOCAL_LIB_DIRNAME, local_nick, LOCAL_FP_SUFFIX)
            remapped += 1
        write_validated(session, pcb, editor.result())

    add_lib_entries(session, "fp", entries)
    return {"libraries": len(entries), "files_copied": copied, "remapped": remapped}


def localize_symbols(session, rows):
    """Localize the libraries of the selected symbols. `rows` are the selected
    scan_items() rows of type 'symbol'.

    Symbols localize per library, not per instance: selecting any symbol of a
    library copies the whole .kicad_sym (so derived symbols keep their 'extends'
    parents) and remaps every instance of that library plus its lib_symbols
    cache entries together. Splitting one library across two nicknames would
    leave the embedded symbol cache inconsistent, so we keep it whole."""
    project_dir = session.project_dir
    resolve = make_resolver(project_dir)

    nicks = set()
    for r in rows:
        if r["type"] == "symbol" and ":" in r["name"]:
            nicks.add(r["name"].split(":", 1)[0])
    if not nicks:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    src_file = {}   # nick -> resolved source .kicad_sym
    for nick in nicks:
        uri = resolve_lib_uri(project_dir, "sym-lib-table", nick)
        if uri:
            src_file[nick] = resolve(uri)
    if not src_file:
        return {"libraries": 0, "files_copied": 0, "remapped": 0}

    entries = {}
    copied = 0
    for nick, sfile in src_file.items():
        local_nick = nick + LOCAL_SUFFIX
        local_lib = os.path.join(project_dir, LOCAL_LIB_DIRNAME, local_nick + ".kicad_sym")
        session.mkdir(os.path.dirname(local_lib))
        if os.path.isfile(sfile) and not os.path.isfile(local_lib):
            shutil.copy2(sfile, local_lib)
            session.wrote_new_file(local_lib)
            copied += 1
        entries[local_nick] = "${{KIPRJMOD}}/{}/{}.kicad_sym".format(
            LOCAL_LIB_DIRNAME, local_nick)

    remapped = 0
    for sch in glob.glob(os.path.join(project_dir, "*.kicad_sch")):
        text = open(sch, encoding="utf-8").read()
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        touched = False

        def remap(atom):
            nonlocal remapped, touched
            if atom.kind != "atom" or ":" not in atom.value:
                return
            nick, name = atom.value.split(":", 1)
            if nick in src_file:
                editor.replace_atom(atom, "{}:{}".format(nick + LOCAL_SUFFIX, name),
                                    force_quote=True)
                remapped += 1
                touched = True

        for lst in root.iter_lists():
            head = lst.head()
            if head == "lib_id" and lst.children[1:]:
                remap(lst.children[1])
            elif head == "lib_symbols":
                # direct child (symbol "nick:name" ...) entries only
                for child in lst.children:
                    if child.kind == "list" and child.head() == "symbol" and child.children[1:]:
                        remap(child.children[1])
        if touched:
            write_validated(session, sch, editor.result())

    add_lib_entries(session, "sym", entries)
    return {"libraries": len(entries), "files_copied": copied, "remapped": remapped}


# --- dispatcher --------------------------------------------------------------

def apply_selection(session, rows):
    """Localize the given selection of scan_items() rows. Symbols and footprints
    are file-level; models are handled here too (file-level) unless the caller
    localizes them live over IPC instead. Returns {scope: summary}."""
    result = {}
    syms = [r for r in rows if r["type"] == "symbol"]
    fps = [r for r in rows if r["type"] == "footprint"]
    models = [r for r in rows if r["type"] == "model"]
    if syms:
        result["symbols"] = localize_symbols(session, syms)
    if fps:
        result["footprints"] = localize_footprints(session, fps)
    if models:
        result["models"] = localize_models(session, models)
    return result
