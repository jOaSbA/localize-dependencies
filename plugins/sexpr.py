# Generic, preserve-everything S-expression parser for KiCad files.
#
# The point is to never corrupt a file. The parser records the exact source span
# of every token, so an edit splices only the span we deliberately change; every
# other byte is preserved verbatim, including tokens and formatting the parser has
# never seen. That is what lets it keep working across KiCad releases: a token
# added in some future version just passes through untouched.

QUOTE = '"'
WHITESPACE = " \t\r\n"


class Node:
    """A parsed S-expr node.

    - A list node has .children (list of Node) and spans '(' .. ')'.
    - An atom node has .value (decoded text) and .quoted (bool); .raw is the
      exact source slice (including quotes/escapes) so it re-emits byte-perfect.
    """
    __slots__ = ("kind", "children", "value", "quoted", "raw", "start", "end")

    def __init__(self, kind):
        self.kind = kind          # "list" or "atom"
        self.children = []
        self.value = None
        self.quoted = False
        self.raw = None
        self.start = -1
        self.end = -1

    # --- navigation helpers ---
    def head(self):
        """The keyword of a list, e.g. 'version' for (version 20250114)."""
        if self.kind == "list" and self.children and self.children[0].kind == "atom":
            return self.children[0].value
        return None

    def find_all(self, keyword):
        """All direct child list-nodes whose head == keyword."""
        return [c for c in self.children
                if c.kind == "list" and c.head() == keyword]

    def find(self, keyword):
        for c in self.children:
            if c.kind == "list" and c.head() == keyword:
                return c
        return None

    def iter_lists(self):
        """Recursively yield every list node in the tree (self included)."""
        if self.kind == "list":
            yield self
            for c in self.children:
                yield from c.iter_lists()


class SExprError(ValueError):
    pass


def _decode(raw):
    """Decode a raw atom slice into its logical string value."""
    if raw and raw[0] == QUOTE and raw[-1] == QUOTE and len(raw) >= 2:
        body = raw[1:-1]
        # KiCad escapes: \" \\ \n \t and unicode. Decode conservatively.
        out = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r",
                            '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out), True
    return raw, False


def parse(text):
    """Parse S-expr text into a single root Node. Raises SExprError on malformed
    input. Guarantees full consumption of the input."""
    n = len(text)
    i = 0
    # skip leading whitespace
    while i < n and text[i] in WHITESPACE:
        i += 1
    if i >= n or text[i] != "(":
        raise SExprError("expected '(' at start of file")
    root, i = _parse_list(text, i)
    # only trailing whitespace allowed
    j = i
    while j < n and text[j] in WHITESPACE:
        j += 1
    if j != n:
        raise SExprError("unexpected trailing content at offset {}".format(j))
    return root


def _parse_list(text, i):
    n = len(text)
    node = Node("list")
    node.start = i
    assert text[i] == "("
    i += 1
    while i < n:
        ch = text[i]
        if ch in WHITESPACE:
            i += 1
            continue
        if ch == ")":
            node.end = i + 1
            return node, i + 1
        if ch == "(":
            child, i = _parse_list(text, i)
            node.children.append(child)
            continue
        # atom
        child, i = _parse_atom(text, i)
        node.children.append(child)
    raise SExprError("unterminated list starting at offset {}".format(node.start))


def _parse_atom(text, i):
    n = len(text)
    start = i
    if text[i] == QUOTE:
        i += 1
        while i < n:
            if text[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if text[i] == QUOTE:
                i += 1
                break
            i += 1
        else:
            raise SExprError("unterminated string at offset {}".format(start))
    else:
        while i < n and text[i] not in WHITESPACE and text[i] not in "()":
            i += 1
    node = Node("atom")
    node.start = start
    node.end = i
    node.raw = text[start:i]
    node.value, node.quoted = _decode(node.raw)
    return node, i


def quote(value):
    """Encode a string value as a KiCad quoted atom."""
    esc = value.replace("\\", "\\\\").replace('"', '\\"')
    esc = esc.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return '"' + esc + '"'


class Editor:
    """Accumulates (start, end, replacement) edits against the original text and
    applies them back-to-front so offsets stay valid. Untouched bytes are kept
    exactly."""
    def __init__(self, text):
        self.text = text
        self._edits = []

    def replace_atom(self, atom, new_value, force_quote=None):
        q = atom.quoted if force_quote is None else force_quote
        rep = quote(new_value) if q else new_value
        self._edits.append((atom.start, atom.end, rep))

    def result(self):
        out = self.text
        for start, end, rep in sorted(self._edits, key=lambda e: e[0], reverse=True):
            out = out[:start] + rep + out[end:]
        return out
