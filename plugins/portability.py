# Project portability engine (file-level, pure Python, no KiCad or wx imports).
#
# Localizes a KiCad project's external dependencies into project-local libraries
# so the project is self-contained and portable: 3D models, footprints, and
# symbols. Operates directly on the saved project files via the generic S-expr
# parser (sexpr.py), editing only the exact byte spans it targets.
#
# SAFETY: before any file is modified it is backed up once, and the manifest
# (.portability/manifest.json) is updated after every change, across all runs.
# Writes are atomic and existing files are never overwritten by copies.
# revert() restores the pre-localize state byte-for-byte, removes everything
# the runs created, and leaves alone files edited since (unless forced).
#
# License: GPL-3.0-or-later

import filecmp
import functools
import glob
import hashlib
import json
import os
import re
import shutil
import sys

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
    # "Install for me only" puts KiCad under the user's own Programs folder.
    # %VAR% only expands on Windows; elsewhere the pattern just matches nothing.
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\KiCad\*\share\kicad"),
    "/usr/share/kicad",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
    # Flatpak: the stock libraries live in the org.kicad.KiCad.Library
    # extension, mounted inside the sandbox the plugin also runs in. Issue #2.
    "/app/extensions/Library",
)
# ${KICAD9_FOOTPRINT_DIR} and friends: version number, then the variable kind.
_VERSIONED_VAR = re.compile(r"KICAD(\d+)_(\w+)")


# --- path resolution ---------------------------------------------------------

def _version_key(path):
    """Sort key: newest KiCad version first. A plain string sort ranks "9.0"
    and "6.0" above "10.0", which picks an old install's libraries."""
    m = re.search(r"(\d+)\.(\d+)", path)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _install_share():
    hits = []
    for pat in _INSTALL_GLOBS:
        hits.extend(p for p in glob.glob(pat) if os.path.isdir(p))
    hits.sort(key=_version_key, reverse=True)
    return hits[0] if hits else None


def _versioned_env(name):
    """Value of the newest set KICAD<n>_<kind> for an unset KICAD<m>_<kind>.
    KiCad resolves an unset ${KICAD9_FOOTPRINT_DIR} through KICAD10_FOOTPRINT_DIR
    this way, so library tables carried over from an older version keep
    working. Issue #2."""
    m = _VERSIONED_VAR.fullmatch(name)
    if not m:
        return None
    hits = []
    for key, val in os.environ.items():
        k = _VERSIONED_VAR.fullmatch(key.upper())
        if val and k and k.group(2) == m.group(2):
            hits.append((int(k.group(1)), val))
    return max(hits)[1] if hits else None


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
            val = os.environ.get(name) or _versioned_env(name)
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
    root = sexpr.parse(_read(path))
    for lib in root.find_all("lib"):
        name = lib.find("name")
        uri = lib.find("uri")
        if name and uri and len(name.children) > 1 and len(uri.children) > 1:
            out[name.children[1].value] = uri.children[1].value
    return out


def _config_base():
    """KiCad's settings root, found the way KiCad finds it: KICAD_CONFIG_HOME
    wins, then the platform's user config dir. Under flatpak XDG_CONFIG_HOME
    points into ~/.var/app/org.kicad.KiCad/config, and ~/.config/kicad belongs
    to some other KiCad install. Issue #2."""
    if os.environ.get("KICAD_CONFIG_HOME"):
        return os.environ["KICAD_CONFIG_HOME"]
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", ""), "kicad")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Preferences/kicad")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME")
                        or os.path.expanduser("~/.config"), "kicad")


def _config_dirs():
    base = _config_base()
    return sorted(glob.glob(os.path.join(base, "*")), key=_version_key,
                  reverse=True) if os.path.isdir(base) else []


@functools.lru_cache(maxsize=64)
def _table_rows(path, stamp):
    """[(nickname, type, uri)] of one table file. `stamp` (mtime, size) keys the
    cache, so an edited table is read again. The scan calls this once per
    footprint, and the stock table alone is ~150 rows."""
    rows = []
    for lib in sexpr.parse(_read(path)).find_all("lib"):
        vals = []
        for key in ("name", "type", "uri"):
            node = lib.find(key)
            vals.append(node.children[1].value
                        if node and len(node.children) > 1 else None)
        rows.append(tuple(vals))
    return rows


def _find_in_table(path, nickname, resolve, seen):
    """uri for `nickname` in one table, following KiCad 10's nested
    (type "Table") entries. The KiCad 10 global table holds the stock libraries
    only through such an entry pointing at the install's template table."""
    path = os.path.normpath(path)
    if path in seen or not os.path.isfile(path):
        return None
    seen.add(path)
    st = os.stat(path)
    nested = []
    for name, typ, uri in _table_rows(path, (st.st_mtime_ns, st.st_size)):
        if typ == "Table":
            if uri:
                nested.append(uri)
        elif name == nickname and uri:
            return uri
    for uri in nested:
        sub = resolve(uri)
        share = _install_share()
        if not os.path.isfile(sub) and share:
            # The stock-table entry holds the absolute path of the install that
            # wrote it, which points nowhere once KiCad is reinstalled elsewhere.
            sub = os.path.join(share, "template", os.path.basename(sub))
        hit = _find_in_table(sub, nickname, resolve, seen)
        if hit:
            return hit
    return None


def resolve_lib_uri(project_dir, table_name, nickname):
    """Resolve a library nickname to its uri the way KiCad does: the project
    table first, then the global table of the newest KiCad version that has
    one. Older versions' tables are left alone; KiCad never reads them, and
    their stale ${KICAD9_*} entries are what issue #2 tripped over."""
    resolve = make_resolver(project_dir)
    tables = [os.path.join(project_dir, table_name)]
    tables += [os.path.join(d, table_name) for d in _config_dirs()
               if os.path.isfile(os.path.join(d, table_name))][:1]
    for t in tables:
        hit = _find_in_table(t, nickname, resolve, set())
        if hit:
            return hit
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
        text = _read(path)
        root = sexpr.parse(text)
        close = root.end - 1                      # offset of final ')'
        nl = "\r\n" if "\r\n" in text else "\n"   # match the file's line endings
        new_text = text[:close] + lines.replace("\n", nl) + nl + text[close:]
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

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read(path):
    """Read a KiCad text file exactly: no newline translation, so CRLF files
    are written back as CRLF."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


class Session:
    """Tracks localize runs for safe, exact revert.

    The manifest accumulates across runs: a file is backed up only the first
    time any run touches it, so Revert always returns to the state before the
    first localize. The manifest is rewritten after every change, so a run that
    fails part-way can still be reverted."""

    def __init__(self, project_dir):
        self.project_dir = os.path.abspath(project_dir)
        self.state_dir = os.path.join(self.project_dir, STATE_DIRNAME)
        self._backed = {}      # rel original path -> backup rel path
        self._created_files = []
        self._created_dirs = []
        self._hashes = {}      # rel path -> sha256 after our last write
        self.summary = {}
        man = _load_manifest(self.project_dir)
        if man:
            for e in man.get("backed_up", []):
                self._backed[e["path"]] = e["backup"]
            self._created_files = list(man.get("created_files", []))
            self._created_dirs = list(man.get("created_dirs", []))
            self._hashes = dict(man.get("hashes", {}))
            self.summary = man.get("summary", {})

    def _rel(self, path):
        return os.path.relpath(os.path.abspath(path),
                               self.project_dir).replace(os.sep, "/")

    def backup(self, path):
        """Copy `path` into the backup store once, before it is modified."""
        rel = self._rel(path)
        if (rel in self._backed or rel in self._created_files
                or not os.path.isfile(path)):
            return
        dest = os.path.join(self.state_dir, "backup", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        self._backed[rel] = "backup/" + rel
        self._save()

    def mkdir(self, path):
        missing = []   # every level makedirs will create, so Revert removes all
        p = os.path.abspath(path)
        while not os.path.isdir(p):
            missing.append(self._rel(p))
            p = os.path.dirname(p)
        if missing:
            os.makedirs(path, exist_ok=True)
            self._created_dirs.extend(missing)
            self._save()

    def wrote_new_file(self, path):
        """Record `path` as created by us. Call BEFORE writing it, so a write
        that dies half-way still gets cleaned up by Revert."""
        rel = self._rel(path)
        if rel not in self._created_files:
            self._created_files.append(rel)
            self._save()

    def written(self, path):
        """Record the content we left in `path`, so Revert can tell whether
        the user changed it afterwards."""
        self._hashes[self._rel(path)] = _sha256(path)
        self._save()

    def _save(self):
        os.makedirs(self.state_dir, exist_ok=True)
        manifest = {
            "backed_up": [{"path": p, "backup": b} for p, b in self._backed.items()],
            "created_files": self._created_files,
            "created_dirs": self._created_dirs,
            "hashes": self._hashes,
            "summary": self.summary,
        }
        mpath = os.path.join(self.state_dir, MANIFEST_NAME)
        with open(mpath + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        os.replace(mpath + ".tmp", mpath)

    def save_manifest(self, summary):
        self.summary = summary
        self._save()


def _load_manifest(project_dir):
    mpath = os.path.join(project_dir, STATE_DIRNAME, MANIFEST_NAME)
    if not os.path.isfile(mpath):
        return None
    with open(mpath, "r", encoding="utf-8") as fh:
        return json.load(fh)


def has_backup(project_dir):
    return os.path.isfile(os.path.join(project_dir, STATE_DIRNAME, MANIFEST_NAME))


def revert_conflicts(project_dir):
    """Files changed since we last wrote them (edited by the user, or by KiCad
    saving over them). Revert would discard those edits."""
    man = _load_manifest(os.path.abspath(project_dir)) or {}
    out = []
    for rel, sha in sorted(man.get("hashes", {}).items()):
        p = os.path.join(project_dir, rel)
        if os.path.isfile(p) and _sha256(p) != sha:
            out.append(rel)
    return out


def revert(project_dir, force=False):
    """Restore backed-up files byte-for-byte and remove created files/dirs.
    Files changed since localize (see revert_conflicts) are left alone and
    reported unless `force`. Returns a summary dict."""
    project_dir = os.path.abspath(project_dir)
    state_dir = os.path.join(project_dir, STATE_DIRNAME)
    man = _load_manifest(project_dir)
    if man is None:
        return {"restored": 0, "removed": 0, "errors": ["No portability backup found."]}

    restored = removed = 0
    errors = []
    skip = set() if force else set(revert_conflicts(project_dir))
    for rel in sorted(skip):
        errors.append("skipped {}: changed since localize".format(rel))

    kept_backed, kept_created = [], []   # still pending after this revert
    for entry in man.get("backed_up", []):
        if entry["path"] in skip:
            kept_backed.append(entry)
            continue
        src = os.path.join(state_dir, entry["backup"])
        dst = os.path.join(project_dir, entry["path"])
        try:
            shutil.copy2(src, dst)
            restored += 1
        except OSError as exc:
            kept_backed.append(entry)
            errors.append("restore {}: {}".format(entry["path"], exc))

    for rel in man.get("created_files", []):
        if rel in skip:
            kept_created.append(rel)
            continue
        p = os.path.join(project_dir, rel)
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
        except OSError as exc:
            kept_created.append(rel)
            errors.append("remove {}: {}".format(rel, exc))

    # deepest dirs first so they are empty when removed
    for rel in sorted(man.get("created_dirs", []), key=len, reverse=True):
        p = os.path.join(project_dir, rel)
        try:
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        except OSError:
            pass

    if not kept_backed and not kept_created:
        shutil.rmtree(state_dir, ignore_errors=True)
    else:
        # keep only what is still pending, so a later (forced) Revert can
        # finish the job without re-flagging files we already restored
        kept = {e["path"] for e in kept_backed} | set(kept_created)
        man["backed_up"] = kept_backed
        man["created_files"] = kept_created
        man["hashes"] = {p: h for p, h in man.get("hashes", {}).items() if p in kept}
        mpath = os.path.join(state_dir, MANIFEST_NAME)
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump(man, fh, indent=2)

    return {"restored": restored, "removed": removed, "errors": errors}


# --- validation --------------------------------------------------------------

def write_validated(session, path, new_text, is_new=False):
    """Write new_text to path, but only after confirming it re-parses. Backs up
    an existing file first. Raises on parse failure (leaving the original).
    The write is atomic: a crash leaves either the old or the new file."""
    sexpr.parse(new_text)  # will raise SExprError if we produced garbage
    if is_new:
        session.wrote_new_file(path)
    else:
        session.backup(path)
    tmp = path + ".portability-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    os.replace(tmp, path)
    session.written(path)


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
        root = sexpr.parse(_read(sch))
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
        root = sexpr.parse(_read(pcb))
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

def _model_sources(resolved):
    """The model file plus its same-stem twins (.step/.wrl/...), one per name."""
    stem = os.path.splitext(os.path.basename(resolved))[0]
    src_dir = os.path.dirname(resolved)
    todo = [resolved]
    for ext in TWIN_EXTS:
        for e in (ext, ext.upper()):
            t = os.path.join(src_dir, stem + e)
            if os.path.isfile(t) and t != resolved:
                todo.append(t)
    out, seen = [], set()
    for src in todo:
        if os.path.basename(src).lower() not in seen:
            seen.add(os.path.basename(src).lower())
            out.append(src)
    return out


def _place_model(session, resolved):
    """Copy a model (and its twins) into packages3D/<*.3dshapes>/ and return
    (kiprjmod_path, files_copied).

    Never overwrites: a destination that already holds identical content is
    reused, and one holding different content (the user's own file, or a
    same-named model from another library) gets a _1, _2, ... suffix instead."""
    project_dir = session.project_dir
    subdir = os.path.basename(os.path.dirname(resolved))
    parts = [LOCAL_3D_DIRNAME] + ([subdir] if subdir else [])
    dest_dir = os.path.join(project_dir, *parts)
    stem, ext = os.path.splitext(os.path.basename(resolved))
    srcs = _model_sources(resolved)

    def dst_for(src, s):
        return os.path.join(dest_dir, s + os.path.splitext(src)[1])

    def fits(s):
        return all(not os.path.exists(dst_for(src, s))
                   or filecmp.cmp(src, dst_for(src, s), shallow=False)
                   for src in srcs)

    n, new_stem = 0, stem
    while not fits(new_stem):
        n += 1
        new_stem = "{}_{}".format(stem, n)

    session.mkdir(dest_dir)
    copied = 0
    for src in srcs:
        dst = dst_for(src, new_stem)
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            continue
        if os.path.exists(dst):   # identical content, reuse it
            continue
        session.wrote_new_file(dst)
        shutil.copy2(src, dst)
        session.written(dst)
        copied += 1
    return "${KIPRJMOD}/" + "/".join(parts + [new_stem + ext]), copied


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
    placed = {}   # normcased source -> new ${KIPRJMOD} path
    for rel, kset in by_pcb.items():
        pcb = os.path.join(project_dir, rel)
        text = _read(pcb)
        root = sexpr.parse(text)
        editor = sexpr.Editor(text)
        for fi, fp in enumerate(root.find_all("footprint")):
            for mi, model in enumerate(fp.find_all("model")):
                if ("model", rel, fi, mi) not in kset or not model.children[1:]:
                    continue
                atom = model.children[1]
                resolved = resolve(atom.value)
                src_key = os.path.normcase(os.path.abspath(resolved))
                if src_key not in placed:
                    placed[src_key], n = _place_model(session, resolved)
                    files_copied += n
                editor.replace_atom(atom, placed[src_key], force_quote=True)
                remapped += 1
        write_validated(session, pcb, editor.result())

    return {"files_copied": files_copied, "remapped": remapped,
            "unique_sources": len(placed)}


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
        text = _read(pcb)
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
                session.wrote_new_file(dst_file)
                shutil.copy2(src_file, dst_file)
                session.written(dst_file)
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
            session.wrote_new_file(local_lib)
            shutil.copy2(sfile, local_lib)
            session.written(local_lib)
            copied += 1
        entries[local_nick] = "${{KIPRJMOD}}/{}/{}.kicad_sym".format(
            LOCAL_LIB_DIRNAME, local_nick)

    remapped = 0
    for sch in glob.glob(os.path.join(project_dir, "*.kicad_sch")):
        text = _read(sch)
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
