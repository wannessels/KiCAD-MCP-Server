"""
Shared text/S-expression helpers for schematic command modules.

These operate directly on ``.kicad_sch`` file text (no pcbnew / KiCADInterface needed)
and are used by the field-layout, batch-authoring and hierarchy command modules. Keeping
them here avoids those modules importing helpers from one another.
"""

import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from utils.sexpr_format import QUOTED_VALUE, unescape_sexpr_string

_KICAD_INTERNAL_PROPS = frozenset(
    {"ki_keywords", "ki_description", "ki_fp_filters", "ki_locked", "ki_model"}
)

# Paper sizes (landscape, mm). Border frame is ~12.7mm from each edge.
_PAPER_DIMS = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
}


def _find_matching_paren(s: str, start: int) -> int:
    """Return index of the ')' matching the '(' at position *start*, or -1."""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _find_placed_symbol_block(
    content: str, reference: str, unit: Optional[int] = None
) -> Tuple[Optional[str], int, int]:
    """Find the placed symbol block for *reference*. Returns (block, start, end) or (None, -1, -1).

    ``unit`` picks one placement of a multi-unit part, whose units all carry the
    same reference; without it the first block in file order wins.
    """
    lib_sym_pos = content.find("(lib_symbols")
    lib_sym_end = _find_matching_paren(content, lib_sym_pos) if lib_sym_pos >= 0 else -1
    pattern = re.compile(r'\(symbol\s+\(lib_id\s+"')
    search_start = 0
    while True:
        m = pattern.search(content, search_start)
        if not m:
            break
        pos = m.start()
        if lib_sym_pos >= 0 and lib_sym_pos <= pos <= lib_sym_end:
            search_start = lib_sym_end + 1
            continue
        end = _find_matching_paren(content, pos)
        if end < 0:
            search_start = pos + 1
            continue
        block_text = content[pos : end + 1]
        if re.search(r'\(property\s+"Reference"\s+"' + re.escape(reference) + r'"', block_text):
            if unit is None:
                return block_text, pos, end
            um = re.search(r"\(unit\s+(\d+)\)", block_text)
            if um and int(um.group(1)) == int(unit):
                return block_text, pos, end
        search_start = end + 1
    return None, -1, -1


def _extract_component_properties(block_text: str, exclude_internal: bool = True) -> Dict[str, str]:
    """Extract {name: value} for all (property "name" "value" ...) entries in a symbol block."""
    props = {}
    for m in re.finditer(r"\(property\s+" + QUOTED_VALUE + r"\s+" + QUOTED_VALUE, block_text):
        name = unescape_sexpr_string(m.group(1))
        value = unescape_sexpr_string(m.group(2))
        if exclude_internal and name in _KICAD_INTERNAL_PROPS:
            continue
        props[name] = value
    return props


def _extract_property_position(block_text: str, property_name: str) -> Optional[Dict[str, float]]:
    """Return {"x","y","angle"} of a named property's (at ...), or None."""
    pat = re.compile(
        r'\(property\s+"'
        + re.escape(property_name)
        + r'"\s+"[^"]*"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)'
    )
    m = pat.search(block_text)
    if m:
        return {"x": float(m.group(1)), "y": float(m.group(2)), "angle": float(m.group(3))}
    return None


def _extract_property_visible(block_text: str, property_name: str) -> bool:
    """True if the named property is visible (no hide flag)."""
    m = re.search(r'\(property\s+"' + re.escape(property_name) + r'"', block_text)
    if not m:
        return True
    end = _find_matching_paren(block_text, m.start())
    if end < 0:
        return True
    prop_sub = block_text[m.start() : end + 1]
    return "(hide yes)" not in prop_sub and "(hide)" not in prop_sub


def _get_sheet_usable_area(schematic_path) -> Tuple[float, float, float, float]:
    """Return (left, top, right, bottom) usable bounds in mm for the sheet's paper size."""
    border = 12.7
    width, height = 297.0, 210.0  # default A4
    try:
        with open(schematic_path, "r", encoding="utf-8") as f:
            content = f.read(4096)
        m = re.search(r'\(paper\s+"([^"]+)"', content)
        if m and m.group(1).strip() in _PAPER_DIMS:
            width, height = _PAPER_DIMS[m.group(1).strip()]
    except Exception:
        pass
    return (border, border, width - border, height - border)


def _apply_visibility(block: str, property_name: str, visible: bool) -> str:
    """Show or hide a field of a placed symbol block.

    KiCad 10 writes a hidden field's flag as a top-level (hide yes) right after its
    (at ...); older formats put it in (effects ...). Every symbol KiCad 10 places
    carries (in_pos_files ...), which tells the two apart.
    """
    ps, pe = _property_span(block, property_name)
    if pe < 0:
        return block
    prop = re.sub(r"\s*\(hide(?:\s+(?:yes|no))?\)", "", block[ps : pe + 1])
    prop = re.sub(r"\)\s+hide(?=\s*\))", ")", prop)  # KiCad 6/7: (effects (font ...) hide)
    if not visible:
        if "(in_pos_files" in block:
            at = re.search(r"(\n[ \t]*)?\(at\s+[^)]*\)", prop)
            if at:
                sep = at.group(1) or " "
                prop = prop[: at.end()] + f"{sep}(hide yes)" + prop[at.end() :]
        else:
            em = re.search(r"\(effects\b", prop)
            if em:
                ee = _find_matching_paren(prop, em.start())
                if ee >= 0:
                    prop = prop[:ee] + " (hide yes)" + prop[ee:]
    return block[:ps] + prop + block[pe + 1 :]


def _property_span(block: str, property_name: str) -> Tuple[int, int]:
    """(start, end) of a property's (property ...) sub-expression in *block*, or (-1, -1)."""
    m = re.search(r'\(property\s+"' + re.escape(property_name) + r'"', block)
    if not m:
        return -1, -1
    return m.start(), _find_matching_paren(block, m.start())


def _property_justify(block: str, property_name: str) -> Optional[str]:
    """The property's (justify ...) tokens, e.g. 'left bottom', or None when centered."""
    ps, pe = _property_span(block, property_name)
    if pe < 0:
        return None
    m = re.search(r"\(justify\s+([^)]*)\)", block[ps : pe + 1])
    return m.group(1).strip() if m else None


def _set_justify(block: str, property_name: str, justify: str) -> str:
    """Set or clear the (justify ...) directive in a property's (effects ...).

    ``justify`` is one or two KiCad alignment keywords ("left", "right top", ...);
    "center" removes the directive, which is how KiCad writes centered text.
    """
    ps, pe = _property_span(block, property_name)
    if pe < 0:
        return block
    em = re.search(r"\(effects\b", block[ps : pe + 1])
    if not em:
        return block
    es = ps + em.start()
    ee = _find_matching_paren(block, es)
    if ee < 0:
        return block
    inner = re.sub(r"\s*\(justify\b[^)]*\)", "", block[es + 1 : ee])
    if not all(t == "center" for t in justify.split()):
        inner = inner.rstrip() + f" (justify {justify.strip()})"
    return block[:es] + "(" + inner + ")" + block[ee + 1 :]


def _move_property_in_block(
    block_text, property_name, x, y, angle=0, visible=None, justify=None
) -> Tuple[str, int]:
    """Place a field of a placed symbol block. Returns (new_block, n_substitutions).

    ``x``/``y``, ``angle`` and ``justify`` are as displayed: angle and justification
    are converted into the symbol's own frame (commands.field_frame), so a rotated or
    mirrored symbol shows the field the way the caller asked. ``justify`` None keeps
    the stored justification; ``visible`` None keeps the field's visibility.
    """
    from commands.field_frame import join_justify, split_justify, stored, symbol_orientation

    rotation, mirror_x, mirror_y = symbol_orientation(block_text)
    keep = _property_justify(block_text, property_name)
    h, v = split_justify(justify if justify is not None else keep)
    stored_angle, sh, sv = stored(float(angle), h, v, rotation, mirror_x, mirror_y)

    prop_pat = re.compile(
        r'(\(property\s+"'
        + re.escape(property_name)
        + r'"\s+"[^"]*"\s+)\(at\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\)'
    )
    new_block, n_subs = prop_pat.subn(r"\g<1>" + f"(at {x} {y} {stored_angle})", block_text)
    if n_subs == 0:
        return block_text, 0
    if justify is not None:
        new_block = _set_justify(new_block, property_name, join_justify(sh, sv))
    if visible is not None:
        new_block = _apply_visibility(new_block, property_name, visible)
    return new_block, n_subs


def _find_project_root(start_dir: Path) -> Path:
    """Walk up from *start_dir* to the nearest dir containing a .kicad_pro (else start_dir)."""
    current = start_dir.resolve()
    while True:
        if list(current.glob("*.kicad_pro")):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start_dir


def _find_facing_label(sch_path, net_name, position, orientation, proximity_mm=14.0):
    """Return [x, y] of an existing label for *net_name* that faces *position*, else None.

    "Facing" = oriented 180° opposite, on the same row (or column), and lying in the
    direction this label would point, within proximity_mm: the two pins face each other
    across a gap, so one straight wire between them is cleaner than two labels. Two
    labels pointing away from each other (pins on opposite sides of one symbol) or
    offset diagonally do not qualify; a wire between them would cross the body.
    """
    try:
        content = sch_path.read_text(encoding="utf-8")
        pat = re.compile(r'\(label\s+"([^"]+)"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+([\d.]+)\)')
        px, py = float(position[0]), float(position[1])
        o = int(round(orientation)) % 360
        facing = (o + 180) % 360
        ux, uy = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}.get(o, (0, 0))
        for m in pat.finditer(content):
            if m.group(1) != net_name:
                continue
            lx, ly, la = float(m.group(2)), float(m.group(3)), float(m.group(4))
            if int(round(la)) % 360 != facing:
                continue
            ahead = (lx - px) * ux + (ly - py) * uy
            aside = abs((lx - px) * uy - (ly - py) * ux)
            if 0 < ahead <= proximity_mm and aside < 0.01:
                return [lx, ly]
    except Exception:
        pass
    return None
