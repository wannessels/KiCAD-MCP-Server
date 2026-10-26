"""Symbol field text: as displayed vs as stored in the .kicad_sch.

eeschema stores a placed symbol's field *position* as drawn, but the field's text
angle and justification in the symbol's own frame. When drawing, it carries the
text box through the symbol's transform (the same one pins get, see
WireDragger.pin_world_xy: rotate screen-CCW, then mirror) and then turns the text
to read left-to-right or bottom-to-top. So on a symbol rotated 90° a field stored
at angle 0 reads vertically, and on a mirrored or 180°-rotated one a field stored
`justify left` hangs to the left of its anchor.

Tools that let a caller place a field take angle and justification as displayed;
``stored`` converts them to what goes in the file, ``displayed`` the other way.
"""

import re
from typing import Optional, Tuple

_H = ("left", "center", "right")
_V = ("top", "center", "bottom")


def _dir(angle: float) -> Tuple[int, int]:
    """Screen (Y-down) unit vector a text at ``angle`` reads along."""
    a = int(round(angle / 90.0)) * 90 % 360
    return {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[a]


def _down(angle: float) -> Tuple[int, int]:
    """Screen unit vector from a text's top towards its baseline."""
    x, y = _dir(angle)
    return -y, x


def _transform(v: Tuple[int, int], rotation: float, mirror_x: bool, mirror_y: bool):
    x, y = v
    for _ in range(int(round(rotation / 90.0)) % 4):
        x, y = y, -x  # eeschema TRANSFORM(0, 1, -1, 0): screen-CCW 90°
    if mirror_x:
        y = -y
    if mirror_y:
        x = -x
    return x, y


def _sign(token: str, positive: str, negative: str) -> int:
    return 1 if token == positive else -1 if token == negative else 0


def displayed(
    angle: float, h: str, v: str, rotation: float, mirror_x: bool = False, mirror_y: bool = False
) -> Tuple[int, str, str]:
    """(angle, h-justify, v-justify) as eeschema draws a field stored with these values."""
    eh = _transform(_dir(angle), rotation, mirror_x, mirror_y)
    ev = _transform(_down(angle), rotation, mirror_x, mirror_y)
    shown = 0 if eh[1] == 0 else 90
    u, d = _dir(shown), _down(shown)
    sh = _sign(h, "left", "right") * (1 if eh == u else -1)
    sv = _sign(v, "top", "bottom") * (1 if ev == d else -1)
    return shown, _H[1 - sh], _V[1 - sv]


def stored(
    angle: float, h: str, v: str, rotation: float, mirror_x: bool = False, mirror_y: bool = False
) -> Tuple[int, str, str]:
    """What to write so eeschema draws the field at (angle, h-justify, v-justify)."""
    a = int((round(angle / 90.0) * 90 - round(rotation / 90.0) * 90) % 180)
    probe = displayed(a, "left", "top", rotation, mirror_x, mirror_y)
    sh = _sign(h, "left", "right") * _sign(probe[1], "left", "right")
    sv = _sign(v, "top", "bottom") * _sign(probe[2], "top", "bottom")
    return a, _H[1 - sh], _V[1 - sv]


def symbol_orientation(block_text: str) -> Tuple[float, bool, bool]:
    """(rotation, mirror_x, mirror_y) of a placed (symbol ...) block's own (at)/(mirror)."""
    at = re.search(r"\(at\s+[-\d.]+\s+[-\d.]+\s+([-\d.]+)\)", block_text)
    mirror = re.search(r"\(mirror\s+([xy])\)", block_text)
    return (
        float(at.group(1)) if at else 0.0,
        bool(mirror and mirror.group(1) == "x"),
        bool(mirror and mirror.group(1) == "y"),
    )


def split_justify(justify: Optional[str]) -> Tuple[str, str]:
    """'right top' -> ('right', 'top'); missing parts are 'center'."""
    tokens = (justify or "").split()
    h = next((t for t in tokens if t in ("left", "right")), "center")
    v = next((t for t in tokens if t in ("top", "bottom")), "center")
    return h, v


def join_justify(h: str, v: str) -> str:
    """('left', 'center') -> 'left'; ('center', 'center') -> 'center'."""
    return " ".join(t for t in (h, v) if t != "center") or "center"
