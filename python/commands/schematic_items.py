"""Text-level edits of top-level items in a .kicad_sch file.

Items are found structurally and edited in place: the rest of the file keeps
its bytes, and an edited item keeps its uuid and every child the edit does not
name. Line endings are kept as found.
"""

import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.file_io import read_text_preserve_newline, write_text_atomic
from utils.sexpr_format import (
    QUOTED_VALUE,
    escape_sexpr_string,
    iter_child_offsets,
    match_paren,
    unescape_sexpr_string,
)

_H_JUSTIFY = ("left", "center", "right")
_V_JUSTIFY = ("top", "bottom")
_TOLERANCE = 0.5


def fmt(v: float) -> str:
    """Format a number the way KiCad writes it (no trailing zeros)."""
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _head_pattern(head: str) -> "re.Pattern[str]":
    return re.compile(r"\(" + re.escape(head) + r"[\s)]")


def top_level_items(content: str, head: str) -> List[Tuple[int, int]]:
    """(start, end) of every direct child of the root list whose head is *head*."""
    pattern = _head_pattern(head)
    return [
        (off, match_paren(content, off))
        for off in iter_child_offsets(content)
        if pattern.match(content, off)
    ]


def child_span(block: str, head: str) -> Tuple[int, int]:
    """(start, end) of the first direct child of *block* whose head is *head*, or (-1, -1)."""
    pattern = _head_pattern(head)
    for off in iter_child_offsets(block):
        if pattern.match(block, off):
            return off, match_paren(block, off)
    return -1, -1


def remove_span(text: str, start: int, end: int) -> str:
    """Cut text[start:end + 1] together with the whitespace that leads up to it."""
    lead = start
    while lead > 0 and text[lead - 1] in " \t\r\n":
        lead -= 1
    return text[:lead] + text[end + 1 :]


def insert_child(block: str, node: str, after: Optional[str] = None) -> str:
    """Append *node* to *block* (or put it right after the child *after*), laid out like
    the block's own children: on its own line when the block spans lines."""
    close = len(block) - 1
    ws = close
    while ws > 0 and block[ws - 1] in " \t\r\n":
        ws -= 1
    trailing = block[ws:close]
    sep = "\n" + trailing.rsplit("\n", 1)[1] + "\t" if "\n" in trailing else " "
    at = ws
    if after is not None:
        _, after_end = child_span(block, after)
        if after_end >= 0:
            at = after_end + 1
    return block[:at] + sep + node + block[at:]


def set_child(block: str, head: str, node: str, after: Optional[str] = None) -> str:
    """Replace the child *head* with *node*, or insert it when there is none."""
    s, e = child_span(block, head)
    if s < 0:
        return insert_child(block, node, after)
    return block[:s] + node + block[e + 1 :]


def remove_child(block: str, head: str) -> str:
    s, e = child_span(block, head)
    return block if s < 0 else remove_span(block, s, e)


def edit_child(block: str, head: str, fn) -> str:
    """Apply *fn* to the text of the child *head*; the block is unchanged when it has none."""
    s, e = child_span(block, head)
    if s < 0:
        return block
    return block[:s] + fn(block[s : e + 1]) + block[e + 1 :]


def _at(block: str) -> Optional[List[float]]:
    s, e = child_span(block, "at")
    if s < 0:
        return None
    return [float(t) for t in block[s + 1 : e].split()[1:]]


def _near(a: List[float], b: List[float]) -> bool:
    return abs(a[0] - b[0]) < _TOLERANCE and abs(a[1] - b[1]) < _TOLERANCE


def _quoted_child(block: str, head: str) -> Optional[str]:
    """The decoded string of a child like (lib_id "Device:R"), or None."""
    s, _ = child_span(block, head)
    if s < 0:
        return None
    m = re.compile(r"\(" + re.escape(head) + r"\s+" + QUOTED_VALUE).match(block, s)
    return unescape_sexpr_string(m.group(1)) if m else None


def _uuid(block: str) -> str:
    s, e = child_span(block, "uuid")
    return block[s + 1 : e].split()[1].strip('"') if s >= 0 else ""


def parse_justify(spec: str) -> Tuple[Optional[str], Optional[str]]:
    """Split "left top", "right", "bottom", ... into (horizontal, vertical); None = not given."""
    h = v = None
    for tok in spec.split():
        if tok in _H_JUSTIFY and h is None:
            h = tok
        elif tok in _V_JUSTIFY and v is None:
            v = tok
        else:
            raise ValueError(
                f"justify '{spec}' is not valid: use one of left/center/right, "
                "optionally followed by top or bottom (e.g. 'left top')"
            )
    if h is None and v is None:
        raise ValueError("justify must not be empty")
    return h, v


def justify_tokens(h: str, v: str) -> str:
    """The (justify ...) contents KiCad writes; it leaves out a centered axis, and its
    parser rejects the keyword "center" (the schematic then fails to load)."""
    return " ".join(t for t in (h, v) if t != "center")


def _current_justify(effects: str) -> Tuple[str, str]:
    s, e = child_span(effects, "justify")
    toks = effects[s + 1 : e].split()[1:] if s >= 0 else []
    h = next((t for t in toks if t in ("left", "right")), "center")
    v = next((t for t in toks if t in _V_JUSTIFY), "center")
    return h, v


def _set_justify(effects: str, h: str, v: str) -> str:
    tokens = justify_tokens(h, v)
    if not tokens:
        return remove_child(effects, "justify")
    return set_child(effects, "justify", f"(justify {tokens})", after="font")


def _text_info(block: str) -> Dict[str, Any]:
    m = re.match(r"\(text\s+" + QUOTED_VALUE, block)
    at = _at(block) or [0.0, 0.0]
    return {
        "text": unescape_sexpr_string(m.group(1)) if m else "",
        "uuid": _uuid(block),
        "position": {"x": at[0], "y": at[1]},
    }


def _select_text(content: str, uid: Optional[str], match: Optional[str]) -> Tuple[int, int]:
    items = [(s, e, _text_info(content[s : e + 1])) for s, e in top_level_items(content, "text")]
    if uid:
        hits = [i for i in items if i[2]["uuid"] == uid]
        what = f"uuid {uid}"
    elif match:
        hits = [i for i in items if i[2]["text"] == match] or [
            i for i in items if match.lower() in i[2]["text"].lower()
        ]
        what = f"'{match}'"
    else:
        raise ValueError("uuid or match is required")
    if not hits:
        raise ValueError(f"No text item matches {what}")
    if len(hits) > 1:
        listing = "; ".join(
            f"{i[2]['uuid']} '{i[2]['text']}' at ({i[2]['position']['x']}, "
            f"{i[2]['position']['y']})"
            for i in hits
        )
        raise ValueError(f"{len(hits)} text items match {what}: {listing}. Pass uuid to pick one.")
    return hits[0][0], hits[0][1]


def _set_flag(font: str, head: str, on: bool) -> str:
    return set_child(font, head, f"({head} yes)") if on else remove_child(font, head)


def edit_text(
    path: Path,
    uid: Optional[str] = None,
    match: Optional[str] = None,
    text: Optional[str] = None,
    position: Optional[List[float]] = None,
    angle: Optional[float] = None,
    font_size: Optional[float] = None,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    justify: Optional[str] = None,
) -> Dict[str, Any]:
    """Change the named attributes of one (text ...) item; returns its info after the edit."""
    content, newline = read_text_preserve_newline(path)
    s, e = _select_text(content, uid, match)
    block = content[s : e + 1]

    if text is not None:
        m = re.match(r"\(text\s+" + QUOTED_VALUE, block)
        block = block[: m.start(1)] + escape_sexpr_string(text) + block[m.end(1) :]
    if position is not None or angle is not None:
        x, y, *rest = _at(block) or [0.0, 0.0]
        if position is not None:
            x, y = position
        a = angle if angle is not None else (rest[0] if rest else 0)
        block = set_child(block, "at", f"(at {fmt(x)} {fmt(y)} {fmt(a)})")
    if font_size is not None:
        size = f"(size {fmt(font_size)} {fmt(font_size)})"
        block = edit_child(
            block,
            "effects",
            lambda ef: edit_child(ef, "font", lambda f: set_child(f, "size", size)),
        )
    for head, on in (("bold", bold), ("italic", italic)):
        if on is not None:
            block = edit_child(
                block,
                "effects",
                lambda ef, head=head, on=on: edit_child(
                    ef, "font", lambda f: _set_flag(f, head, on)
                ),
            )
    if justify is not None:
        h, v = parse_justify(justify)

        def _apply(ef: str) -> str:
            h0, v0 = _current_justify(ef)
            return _set_justify(ef, h or h0, v or v0)

        block = edit_child(block, "effects", _apply)

    content = content[:s] + block + content[e + 1 :]
    write_text_atomic(path, content, newline)
    return _text_info(block)


def delete_text(path: Path, uid: Optional[str] = None, match: Optional[str] = None) -> Dict:
    """Remove one (text ...) item; returns the info of the removed item."""
    content, newline = read_text_preserve_newline(path)
    s, e = _select_text(content, uid, match)
    info = _text_info(content[s : e + 1])
    write_text_atomic(path, remove_span(content, s, e), newline)
    return info


def delete_items_at(path: Path, head: str, position: List[float]) -> int:
    """Remove every top-level *head* item (no_connect, junction) at *position*;
    returns how many were removed."""
    content, newline = read_text_preserve_newline(path)
    spans = [
        (s, e)
        for s, e in top_level_items(content, head)
        if _near(_at(content[s : e + 1]) or [float("inf")] * 2, position)
    ]
    for s, e in reversed(spans):
        content = remove_span(content, s, e)
    if spans:
        write_text_atomic(path, content, newline)
    return len(spans)


def add_junction(path: Path, position: List[float]) -> bool:
    """Add a junction dot at *position*; False when one is already there."""
    from commands.wire_manager import _text_insert

    content, _ = read_text_preserve_newline(path)
    if any(
        _near(_at(content[s : e + 1]), position) for s, e in top_level_items(content, "junction")
    ):
        return False
    x, y = fmt(position[0]), fmt(position[1])
    _text_insert(
        path,
        f"\t(junction\n\t\t(at {x} {y})\n\t\t(diameter 0)\n\t\t(color 0 0 0 0)\n"
        f'\t\t(uuid "{uuid.uuid4()}")\n\t)\n',
    )
    return True


def stale_junctions(path: Path) -> List[Dict[str, Any]]:
    """Junctions that join fewer than three things: wire or bus ends and pins count
    one each, a wire running straight through counts two (it is split there)."""
    import sexpdata
    from commands.wire_manager import WireManager

    def key(x: float, y: float) -> Tuple[int, int]:
        return round(x * 10000), round(y * 10000)

    content, _ = read_text_preserve_newline(path)
    data = sexpdata.loads(content)
    ends: Counter = Counter()
    segments = []
    for item in data:
        if not (isinstance(item, list) and item and str(item[0]) in ("wire", "bus")):
            continue
        pts = next((p for p in item[1:] if isinstance(p, list) and p and str(p[0]) == "pts"), [])
        xy = [(float(p[1]), float(p[2])) for p in pts[1:] if isinstance(p, list)]
        if len(xy) < 2:
            continue
        ends[key(*xy[0])] += 1
        ends[key(*xy[-1])] += 1
        segments.append((*xy[0], *xy[-1]))
    pins = Counter(key(x, y) for x, y in WireManager._collect_pin_positions(data))

    stale = []
    for s, e in top_level_items(content, "junction"):
        block = content[s : e + 1]
        x, y = _at(block)[:2]
        through = sum(WireManager._point_strictly_on_wire(x, y, *seg) for seg in segments)
        connections = ends[key(x, y)] + pins[key(x, y)] + 2 * through
        if connections < 3:
            stale.append(
                {"position": {"x": x, "y": y}, "uuid": _uuid(block), "connections": connections}
            )
    return stale


_FLAG_ORDER = ("exclude_from_sim", "in_bom", "on_board", "in_pos_files", "dnp")


def _flag_anchor(block: str, head: str) -> Optional[str]:
    """The child a missing flag goes after, so it lands where KiCad writes it."""
    before = _FLAG_ORDER[: _FLAG_ORDER.index(head)][::-1] + ("body_style", "unit", "at")
    return next((h for h in before if child_span(block, h)[0] >= 0), None)


def set_component_flags(path: Path, reference: str, flags: Dict[str, bool]) -> int:
    """Set dnp / in_bom / on_board / exclude_from_sim on every placed unit of
    *reference*; returns how many units were changed."""
    content, newline = read_text_preserve_newline(path)
    ref = re.compile(
        r'\(property\s+"Reference"\s+"' + re.escape(escape_sexpr_string(reference)) + '"'
    )
    spans = [(s, e) for s, e in top_level_items(content, "symbol") if ref.search(content, s, e)]
    if not spans:
        raise ValueError(f"Component '{reference}' not found in schematic")
    for s, e in reversed(spans):
        block = content[s : e + 1]
        for head, on in flags.items():
            node = f"({head} {'yes' if on else 'no'})"
            block = set_child(block, head, node, after=_flag_anchor(block, head))
        content = content[:s] + block + content[e + 1 :]
    write_text_atomic(path, content, newline)
    return len(spans)


_TITLE_FIELDS = ("title", "date", "rev", "company")
_TITLE_FIELD = re.compile(r"\((title|date|rev|company)\s+" + QUOTED_VALUE + r"\)")
_TITLE_COMMENT = re.compile(r"\(comment\s+(\d+)\s+" + QUOTED_VALUE + r"\)")


def set_title_block(path: Path, fields: Dict[str, str], comments: Dict[int, str]) -> Dict[str, Any]:
    """Merge *fields* (title, date, rev, company) and *comments* (1-9) into the sheet's
    title block; an empty string clears a field. Returns the resulting title block."""
    content, newline = read_text_preserve_newline(path)
    spans = top_level_items(content, "title_block")
    current: Dict[str, str] = {}
    current_comments: Dict[int, str] = {}
    if spans:
        block = content[spans[0][0] : spans[0][1] + 1]
        for off in iter_child_offsets(block):
            if m := _TITLE_FIELD.match(block, off):
                current[m.group(1)] = unescape_sexpr_string(m.group(2))
            elif m := _TITLE_COMMENT.match(block, off):
                current_comments[int(m.group(1))] = unescape_sexpr_string(m.group(2))
    current.update(fields)
    current_comments.update(comments)

    lines = [f'({k} "{escape_sexpr_string(current[k])}")' for k in _TITLE_FIELDS if current.get(k)]
    lines += [
        f'(comment {n} "{escape_sexpr_string(v)}")'
        for n, v in sorted(current_comments.items())
        if v
    ]
    node = "(title_block" + "".join(f"\n\t\t{line}" for line in lines) + "\n\t)"
    if spans:
        s, e = spans[0]
        content = remove_span(content, s, e) if not lines else content[:s] + node + content[e + 1 :]
    elif lines:
        _, paper_end = top_level_items(content, "paper")[0]
        content = content[: paper_end + 1] + "\n\t" + node + content[paper_end + 1 :]
    write_text_atomic(path, content, newline)
    return {
        **{k: current[k] for k in _TITLE_FIELDS if current.get(k)},
        "comments": {n: v for n, v in sorted(current_comments.items()) if v},
    }


def purge_unused_lib_symbols(path: Path) -> List[str]:
    """Drop (lib_symbols ...) entries no placed symbol uses, as eeschema does on save;
    returns the names removed. A placed symbol uses its lib_name, else its lib_id."""
    content, newline = read_text_preserve_newline(path)
    used = set()
    for s, e in top_level_items(content, "symbol"):
        block = content[s : e + 1]
        used.add(_quoted_child(block, "lib_name") or _quoted_child(block, "lib_id"))
    libs = top_level_items(content, "lib_symbols")
    if not libs:
        return []
    ls, le = libs[0]
    lib_block = content[ls : le + 1]
    entry = re.compile(r"\(symbol\s+" + QUOTED_VALUE)
    unused = []
    for off in iter_child_offsets(lib_block):
        m = entry.match(lib_block, off)
        if m and unescape_sexpr_string(m.group(1)) not in used:
            unused.append((off, match_paren(lib_block, off), unescape_sexpr_string(m.group(1))))
    if not unused:
        return []
    for s, e, _ in reversed(unused):
        lib_block = remove_span(lib_block, s, e)
    if next(iter_child_offsets(lib_block), None) is None:
        lib_block = "(lib_symbols)"
    content = content[:ls] + lib_block + content[le + 1 :]
    write_text_atomic(path, content, newline)
    return [name for _, _, name in unused]
