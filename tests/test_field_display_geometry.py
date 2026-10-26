"""Fields and labels land where eeschema draws them.

eeschema stores a placed symbol's field position as drawn but its text angle and
justification in the symbol's frame, and it applies a (mirror) token to the field
positions it has already read. The truth table below was read off kicad-cli 10.0.7
renders of real sheets; the rest checks that the tools write files that render the
way the caller asked:

- field angle/justify given as displayed are stored in the symbol's frame,
- library field offsets get the Y-flip (a GND's name goes under the glyph),
- batch placement leaves power symbols' hidden #PWR reference alone,
- rotate/mirror writes (mirror) right after (at) and turns the fields with the symbol,
- a "facing" label must be in line and pointing back, never across a symbol.
"""

import re
import sys
from pathlib import Path

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

pytestmark = pytest.mark.unit

from commands.field_frame import displayed, stored  # noqa: E402
from commands.schematic_text_utils import (  # noqa: E402
    _extract_property_position,
    _find_facing_label,
    _move_property_in_block,
    _property_justify,
)
from commands.wire_dragger import WireDragger  # noqa: E402

# (stored angle, justify, symbol rotation, mirror_x, mirror_y) -> (shown angle, justify)
KICAD_RENDERED = [
    ((0, "right", 0, False, True), (0, "left")),  # INA228 mirrored left-right
    ((0, "right", 180, False, False), (0, "left")),  # solder jumper turned 180
    ((90, "left", 90, False, False), (0, "right")),  # solder jumper turned 90
    ((90, "right", 90, False, False), (0, "left")),  # GND turned to point right
    ((270, "left", 270, False, False), (0, "right")),  # GND turned to point left
    ((90, "center", 90, False, False), (0, "center")),  # resistor turned 90
    ((0, "center", 90, False, False), (90, "center")),  # ... reads vertically
]


@pytest.mark.parametrize("stored_field,shown", KICAD_RENDERED)
def test_displayed_matches_kicad(stored_field, shown):
    angle, h, rot, mx, my = stored_field
    assert displayed(angle, h, "center", rot, mx, my)[:2] == shown


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", [None, "x", "y"])
@pytest.mark.parametrize("angle", [0, 90])
@pytest.mark.parametrize("h", ["left", "center", "right"])
@pytest.mark.parametrize("v", ["top", "center", "bottom"])
def test_stored_is_drawn_as_asked(rot, mirror, angle, h, v):
    mx, my = mirror == "x", mirror == "y"
    assert displayed(*stored(angle, h, v, rot, mx, my), rot, mx, my) == (angle, h, v)


def _block(rotation=0, mirror=None, ref_hidden=False):
    mirror_line = f"\n    (mirror {mirror})" if mirror else ""
    hide = "\n      (hide yes)" if ref_hidden else ""
    return (
        f'(symbol (lib_id "Device:R") (at 50 50 {rotation}){mirror_line}\n'
        f'    (property "Reference" "R1" (at 52 47 0){hide}\n'
        "      (effects (font (size 1.27 1.27)))\n    )\n"
        '    (property "Value" "10k" (at 52 53 0)\n'
        "      (effects (font (size 1.27 1.27)))\n    )\n)"
    )


def _field(block, name):
    at = _extract_property_position(block, name)
    return (at["x"], at["y"], at["angle"]), _property_justify(block, name)


class TestMovePropertyInBlock:
    def test_horizontal_on_a_rotated_symbol_is_stored_at_90(self):
        block, n = _move_property_in_block(_block(rotation=90), "Reference", 55, 50, 0)
        assert n == 1
        assert _field(block, "Reference")[0] == (55.0, 50.0, 90.0)

    def test_justify_is_converted_for_a_mirrored_symbol(self):
        block, _ = _move_property_in_block(
            _block(mirror="y"), "Reference", 45, 50, 0, justify="right"
        )
        assert _field(block, "Reference")[1] == "left"

    def test_left_on_a_symbol_turned_90_is_stored_right(self):
        block, _ = _move_property_in_block(_block(rotation=90), "Value", 55, 50, 0, justify="left")
        assert _field(block, "Value") == ((55.0, 50.0, 90.0), "right")

    def test_visibility_kept_unless_asked(self):
        block, _ = _move_property_in_block(_block(ref_hidden=True), "Reference", 1, 2, 0)
        assert "(hide yes)" in block.split('"Value"')[0]
        block, _ = _move_property_in_block(block, "Reference", 1, 2, 0, visible=True)
        assert "(hide yes)" not in block.split('"Value"')[0]


def test_edit_schematic_component_field_positions_are_as_displayed(tmp_path):
    from kicad_interface import KiCADInterface

    sch = tmp_path / "t.kicad_sch"
    sch.write_text(
        "(kicad_sch (version 20250114)\n  (lib_symbols)\n  " + _block(rotation=90) + "\n)\n",
        encoding="utf-8",
    )
    iface = KiCADInterface.__new__(KiCADInterface)
    r = iface._handle_edit_schematic_component(
        {
            "schematicPath": str(sch),
            "reference": "R1",
            "fieldPositions": {"Reference": {"x": 54, "y": 50, "angle": 0, "justify": "left"}},
        }
    )
    assert r["success"], r
    assert _field(sch.read_text(encoding="utf-8"), "Reference") == ((54.0, 50.0, 90.0), "right")


GND_SCH = """(kicad_sch (version 20231120) (generator test)
  (lib_symbols
    (symbol "power:GND" (power) (in_bom no) (on_board yes)
      (property "Reference" "#PWR" (at 0 -6.35 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Value" "GND" (at 0 -3.81 0) (effects (font (size 1.27 1.27))))
    )
  )
)
"""


def test_library_field_offset_is_y_flipped(tmp_path):
    """power:GND keeps its Value 3.81 mm *below* the glyph, as in eeschema."""
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    sch = tmp_path / "t.kicad_sch"
    sch.write_text(GND_SCH, encoding="utf-8")
    DynamicSymbolLoader(project_path=tmp_path).create_component_instance(
        sch, "power", "GND", reference="#PWR01", value="GND", x=100.33, y=100.33
    )
    placed = sch.read_text(encoding="utf-8").split("(symbol (lib_id", 1)[1]
    assert re.search(r'"Value" "GND"\s+\(at 100.33 104.14 ', placed), placed


def test_batch_add_leaves_power_symbol_fields_alone(tmp_path, monkeypatch):
    import types

    from commands import schematic_batch as sb

    sch = tmp_path / "t.kicad_sch"
    block = (
        '(symbol (lib_id "power:GND") (at 10 10 0) (unit 1)\n'
        '    (property "Reference" "#PWR01" (at 10 3.65 0)\n      (hide yes)\n'
        "      (effects (font (size 1.27 1.27)))\n    )\n"
        '    (property "Value" "GND" (at 10 13.81 0)\n'
        "      (effects (font (size 1.27 1.27)))\n    )\n)"
    )
    sch.write_text("(kicad_sch\n  " + block + "\n)\n", encoding="utf-8")
    monkeypatch.setattr(sb.DynamicSymbolLoader, "add_component", lambda *a, **k: True)
    monkeypatch.setattr(
        sb,
        "PinLocator",
        lambda: types.SimpleNamespace(
            _schematic_cache={}, get_all_symbol_pins=lambda p, ref: {"1": [10, 10]}
        ),
    )
    sb.SchematicBatchCommands(types.SimpleNamespace()).batch_add_components(
        {
            "schematicPath": str(sch),
            "components": [
                {"symbol": "power:GND", "reference": "#PWR01", "position": {"x": 10, "y": 10}}
            ],
        }
    )
    text = sch.read_text(encoding="utf-8")
    assert re.search(r'"Reference" "#PWR01"\s+\(at 10 3.65 0\)\s+\(hide yes\)', text), text
    assert '"Value" "GND" (at 10 13.81 0)' in text


def _sch_with(block):
    return sexpdata.loads(f"(kicad_sch (lib_symbols) {block})")


def _symbol(sch):
    return next(i for i in sch if isinstance(i, list) and i[0] == sexpdata.Symbol("symbol"))


def _tags(sym):
    return [str(s[0]) for s in sym[1:] if isinstance(s, list)]


def _at(sym, prop):
    p = next(
        s for s in sym if isinstance(s, list) and s and str(s[0]) == "property" and s[1] == prop
    )
    a = next(s for s in p if isinstance(s, list) and str(s[0]) == "at")
    return float(a[1]), float(a[2])


class TestRotateMirror:
    def test_mirror_written_right_after_at(self):
        sch = _sch_with(_block())
        WireDragger.update_symbol_rotation_mirror(sch, "R1", 0, "x")
        assert _tags(_symbol(sch))[:3] == ["lib_id", "at", "mirror"]

    def test_fields_turn_with_the_symbol(self):
        sch = _sch_with(_block())
        WireDragger.update_symbol_rotation_mirror(sch, "R1", 90, None)
        # (2, -3) from the origin turned screen-CCW 90° is (-3, -2)
        assert _at(_symbol(sch), "Reference") == (47.0, 48.0)

    def test_mirror_flips_the_fields(self):
        sch = _sch_with(_block())
        WireDragger.update_symbol_rotation_mirror(sch, "R1", 0, "y")
        assert _at(_symbol(sch), "Reference") == (48.0, 47.0)

    def test_a_trailing_mirror_token_keeps_the_fields_where_they_were_drawn(self):
        """Files written before the fix carry (mirror) last, so eeschema drew their
        fields mirrored; moving the token must not move what the reader sees."""
        legacy = _block().replace("\n)", "\n    (mirror x)\n)")
        sch = _sch_with(legacy)
        WireDragger.update_symbol_rotation_mirror(sch, "R1", 0, "x")
        sym = _symbol(sch)
        assert _tags(sym)[:3] == ["lib_id", "at", "mirror"]
        assert _at(sym, "Reference") == (52.0, 53.0)


class TestFacingLabel:
    def _sch(self, tmp_path, label_at):
        f = tmp_path / "s.kicad_sch"
        f.write_text(f'(kicad_sch (label "N" (at {label_at})))')
        return f

    def test_pins_facing_across_a_gap(self, tmp_path):
        f = self._sch(tmp_path, "10 20 0")
        assert _find_facing_label(f, "N", [20, 20], orientation=180) == [10.0, 20.0]

    def test_labels_pointing_away_are_not_facing(self, tmp_path):
        """Pins on the two sides of one connector: a wire would cross the body."""
        f = self._sch(tmp_path, "213.36 91.44 0")
        assert _find_facing_label(f, "N", [200.66, 93.98], orientation=180) is None
        f = self._sch(tmp_path, "213.36 93.98 0")
        assert _find_facing_label(f, "N", [200.66, 93.98], orientation=180) is None

    def test_diagonal_is_not_facing(self, tmp_path):
        f = self._sch(tmp_path, "10 22.54 0")
        assert _find_facing_label(f, "N", [20, 20], orientation=180) is None
