"""
Tests for edit_schematic_text, delete_schematic_text and delete_schematic_no_connect.

Covers:
  - lookup by uuid, exact text, substring; ambiguity and not-found errors
  - in-place edits keep the uuid, untouched children and the rest of the file
  - KiCad escaping of new text, KiCad justify keywords
  - CRLF / LF line endings kept as found
  - no-connect removal by position and by componentRef + pin
"""

import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.fixture()
def iface():
    return _make_iface()


_UUID_NOTE = "aaaaaaaa-0000-0000-0000-000000000011"
_UUID_POWER = "aaaaaaaa-0000-0000-0000-000000000012"

_SCH = textwrap.dedent("""\
    (kicad_sch
    \t(version 20260306)
    \t(generator "eeschema")
    \t(generator_version "10.0")
    \t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
    \t(paper "A4")
    \t(lib_symbols
    \t\t(symbol "Dev:Thing"
    \t\t\t(symbol "Thing_0_1"
    \t\t\t\t(text "Note"
    \t\t\t\t\t(at 0 0 0)
    \t\t\t\t\t(effects
    \t\t\t\t\t\t(font
    \t\t\t\t\t\t\t(size 1.27 1.27)
    \t\t\t\t\t\t)
    \t\t\t\t\t)
    \t\t\t\t)
    \t\t\t)
    \t\t)
    \t)
    \t(text "Note (rev A)"
    \t\t(exclude_from_sim no)
    \t\t(at 50 60 0)
    \t\t(effects
    \t\t\t(font
    \t\t\t\t(size 1.27 1.27)
    \t\t\t\t(color 255 0 0 1)
    \t\t\t)
    \t\t\t(justify left bottom)
    \t\t)
    \t\t(uuid "aaaaaaaa-0000-0000-0000-000000000011")
    \t)
    \t(text "Power section"
    \t\t(exclude_from_sim no)
    \t\t(at 80 20 0)
    \t\t(effects
    \t\t\t(font
    \t\t\t\t(size 2 2)
    \t\t\t\t(bold yes)
    \t\t\t)
    \t\t)
    \t\t(uuid "aaaaaaaa-0000-0000-0000-000000000012")
    \t)
    \t(no_connect
    \t\t(at 20 20)
    \t\t(uuid "aaaaaaaa-0000-0000-0000-000000000021")
    \t)
    \t(no_connect
    \t\t(at 30 20)
    \t\t(uuid "aaaaaaaa-0000-0000-0000-000000000022")
    \t)
    \t(sheet_instances
    \t\t(path "/"
    \t\t\t(page "1")
    \t\t)
    \t)
    )
    """)


def _write(tmp_path, content: str = _SCH, crlf: bool = False) -> Path:
    sch = tmp_path / "test.kicad_sch"
    data = content.replace("\n", "\r\n") if crlf else content
    sch.write_bytes(data.encode("utf-8"))
    return sch


def _read(sch: Path) -> str:
    return sch.read_bytes().decode("utf-8")


def _block(content: str, uid: str) -> str:
    from commands.schematic_items import top_level_items

    for s, e in top_level_items(content, "text"):
        if uid in content[s : e + 1]:
            return content[s : e + 1]
    raise AssertionError(f"no text item with uuid {uid}")


@pytest.mark.unit
class TestTextLookup:
    def test_by_uuid(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        item = edit_text(sch, uid=_UUID_POWER, angle=90)
        assert item["text"] == "Power section"

    def test_exact_match_beats_substring(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path, _SCH.replace("Power section", "Note"))
        item = edit_text(sch, match="Note", angle=90)
        assert item["uuid"] == _UUID_POWER

    def test_substring_is_case_insensitive(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        assert edit_text(sch, match="rev a", angle=90)["uuid"] == _UUID_NOTE

    def test_ignores_text_inside_lib_symbols(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        # The library graphic "Note" is an exact match but not a sheet item;
        # the substring match on the sheet's own text is the only candidate.
        assert edit_text(sch, match="Note", angle=90)["uuid"] == _UUID_NOTE

    def test_several_matches_is_an_error(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        before = _read(sch)
        with pytest.raises(ValueError, match="2 text items match") as exc:
            edit_text(sch, match="o", angle=90)
        assert _UUID_NOTE in str(exc.value) and _UUID_POWER in str(exc.value)
        assert _read(sch) == before

    def test_no_match_is_an_error(self, tmp_path):
        from commands.schematic_items import delete_text

        sch = _write(tmp_path)
        with pytest.raises(ValueError, match="No text item"):
            delete_text(sch, match="nothing like this")

    def test_lookup_required(self, tmp_path):
        from commands.schematic_items import delete_text

        with pytest.raises(ValueError, match="uuid or match"):
            delete_text(_write(tmp_path))


@pytest.mark.unit
class TestEditText:
    def test_new_text_is_escaped_like_kicad(self, tmp_path):
        from commands.schematic_items import edit_text
        from commands.wire_manager import WireManager

        sch = _write(tmp_path)
        new = 'two\nlines, a "quote" and C:\\path'
        edit_text(sch, uid=_UUID_NOTE, text=new)

        assert r'(text "two\nlines, a \"quote\" and C:\\path"' in _read(sch)
        texts = {t["uuid"]: t["text"] for t in WireManager.list_texts(sch)}
        assert texts[_UUID_NOTE] == new

    def test_only_the_item_changes(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_NOTE, position=[12.7, 25.4])

        expected = _SCH.replace("(at 50 60 0)", "(at 12.7 25.4 0)")
        assert _read(sch) == expected

    def test_angle_keeps_position(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_NOTE, angle=90)
        assert "(at 50 60 90)" in _block(_read(sch), _UUID_NOTE)

    def test_font_size_keeps_color(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_NOTE, font_size=2.54)
        block = _block(_read(sch), _UUID_NOTE)
        assert "(size 2.54 2.54)" in block
        assert "(color 255 0 0 1)" in block

    def test_bold_and_italic_on_and_off(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_NOTE, bold=True, italic=True)
        block = _block(_read(sch), _UUID_NOTE)
        assert "\t\t\t\t(color 255 0 0 1)\n\t\t\t\t(bold yes)\n\t\t\t\t(italic yes)\n" in block

        edit_text(sch, uid=_UUID_POWER, bold=False)
        assert "bold" not in _block(_read(sch), _UUID_POWER)

    def test_justify_both_axes(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_NOTE, justify="right top")
        assert "(justify right top)" in _block(_read(sch), _UUID_NOTE)

    def test_justify_vertical_only_keeps_horizontal(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_NOTE, justify="top")
        assert "(justify left top)" in _block(_read(sch), _UUID_NOTE)

    def test_justify_center_writes_no_center_keyword(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_NOTE, justify="center")
        block = _block(_read(sch), _UUID_NOTE)
        assert "(justify bottom)" in block
        assert "center" not in block

    def test_justify_inserted_after_font(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_POWER, justify="left top")
        block = _block(_read(sch), _UUID_POWER)
        assert "\t\t\t\t(bold yes)\n\t\t\t)\n\t\t\t(justify left top)\n\t\t)" in block

    def test_invalid_justify(self, tmp_path):
        from commands.schematic_items import edit_text

        with pytest.raises(ValueError, match="justify"):
            edit_text(_write(tmp_path), uid=_UUID_NOTE, justify="middle")

    def test_keeps_uuid_and_parses(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        item = edit_text(sch, uid=_UUID_NOTE, text="X", position=[1, 2], angle=180)
        assert item["uuid"] == _UUID_NOTE
        assert item["position"] == {"x": 1.0, "y": 2.0}
        sexpdata.loads(_read(sch))

    def test_keeps_crlf(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path, crlf=True)
        edit_text(sch, uid=_UUID_NOTE, bold=True, justify="right top")
        raw = sch.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n")
        assert b"(bold yes)\r\n" in raw

    def test_keeps_lf(self, tmp_path):
        from commands.schematic_items import edit_text

        sch = _write(tmp_path)
        edit_text(sch, uid=_UUID_NOTE, bold=True)
        assert b"\r" not in sch.read_bytes()


@pytest.mark.unit
class TestDeleteText:
    def test_removes_only_that_item(self, tmp_path):
        from commands.schematic_items import delete_text

        sch = _write(tmp_path)
        info = delete_text(sch, match="power")
        assert info["uuid"] == _UUID_POWER

        content = _read(sch)
        assert _UUID_POWER not in content
        block = _block(_SCH, _UUID_POWER)
        assert content == _SCH.replace("\n\t" + block, "")

    def test_keeps_crlf(self, tmp_path):
        from commands.schematic_items import delete_text

        sch = _write(tmp_path, crlf=True)
        delete_text(sch, uid=_UUID_NOTE)
        raw = sch.read_bytes()
        assert _UUID_NOTE.encode() not in raw
        assert raw.count(b"\n") == raw.count(b"\r\n")


@pytest.mark.unit
class TestListTextsJustify:
    def test_reports_both_axes(self, tmp_path):
        from commands.wire_manager import WireManager

        texts = {t["uuid"]: t for t in WireManager.list_texts(_write(tmp_path))}
        assert texts[_UUID_NOTE]["justify"] == "left"
        assert texts[_UUID_NOTE]["vertical_justify"] == "bottom"
        assert texts[_UUID_POWER]["justify"] == "center"
        assert texts[_UUID_POWER]["vertical_justify"] == "center"


@pytest.mark.unit
class TestHandleEditDeleteText:
    def test_edit_success(self, iface, tmp_path):
        sch = _write(tmp_path)
        result = iface._handle_edit_schematic_text(
            {"schematicPath": str(sch), "match": "power", "italic": True}
        )
        assert result["success"] is True
        assert result["item"]["uuid"] == _UUID_POWER
        assert "(italic yes)" in _block(_read(sch), _UUID_POWER)

    def test_edit_nothing_to_change(self, iface, tmp_path):
        result = iface._handle_edit_schematic_text(
            {"schematicPath": str(_write(tmp_path)), "uuid": _UUID_NOTE}
        )
        assert result["success"] is False
        assert "Nothing to change" in result["message"]

    def test_edit_ambiguous_reports_candidates(self, iface, tmp_path):
        result = iface._handle_edit_schematic_text(
            {"schematicPath": str(_write(tmp_path)), "match": "o", "bold": True}
        )
        assert result["success"] is False
        assert _UUID_NOTE in result["message"]

    def test_edit_invalid_justify(self, iface, tmp_path):
        result = iface._handle_edit_schematic_text(
            {"schematicPath": str(_write(tmp_path)), "uuid": _UUID_NOTE, "justify": "up"}
        )
        assert result["success"] is False
        assert "justify" in result["message"]

    def test_edit_missing_file(self, iface, tmp_path):
        result = iface._handle_edit_schematic_text(
            {"schematicPath": str(tmp_path / "nope.kicad_sch"), "uuid": "x", "bold": True}
        )
        assert result["success"] is False

    def test_delete_success(self, iface, tmp_path):
        sch = _write(tmp_path)
        result = iface._handle_delete_schematic_text(
            {"schematicPath": str(sch), "uuid": _UUID_NOTE}
        )
        assert result["success"] is True
        assert _UUID_NOTE not in _read(sch)

    def test_delete_not_found(self, iface, tmp_path):
        result = iface._handle_delete_schematic_text(
            {"schematicPath": str(_write(tmp_path)), "match": "zzz"}
        )
        assert result["success"] is False


@pytest.mark.unit
class TestDeleteNoConnect:
    def test_by_position(self, tmp_path):
        from commands.schematic_items import delete_items_at

        sch = _write(tmp_path)
        assert delete_items_at(sch, "no_connect", [20.0, 20.0]) == 1
        content = _read(sch)
        assert "000000000021" not in content
        assert "000000000022" in content
        sexpdata.loads(content)

    def test_removes_stacked_duplicates(self, tmp_path):
        from commands.schematic_items import delete_items_at

        sch = _write(tmp_path, _SCH.replace("(at 30 20)", "(at 20 20)"))
        assert delete_items_at(sch, "no_connect", [20.0, 20.0]) == 2
        assert "(no_connect" not in _read(sch)

    def test_nothing_there_leaves_file_alone(self, tmp_path):
        from commands.schematic_items import delete_items_at

        sch = _write(tmp_path)
        assert delete_items_at(sch, "no_connect", [99.0, 99.0]) == 0
        assert _read(sch) == _SCH

    def test_keeps_crlf(self, tmp_path):
        from commands.schematic_items import delete_items_at

        sch = _write(tmp_path, crlf=True)
        delete_items_at(sch, "no_connect", [30.0, 20.0])
        raw = sch.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n")

    def test_handler_by_position(self, iface, tmp_path):
        sch = _write(tmp_path)
        result = iface._handle_delete_schematic_no_connect(
            {"schematicPath": str(sch), "position": [30, 20]}
        )
        assert result["success"] is True
        assert result["removed"] == 1

    def test_handler_by_pin(self, iface, tmp_path):
        sch = _write(tmp_path)
        with patch(
            "commands.pin_locator.PinLocator.get_pin_location", return_value=[20.0, 20.0]
        ) as locate:
            result = iface._handle_delete_schematic_no_connect(
                {"schematicPath": str(sch), "componentRef": "U1", "pinNumber": 3}
            )
        locate.assert_called_once_with(sch, "U1", "3")
        assert result["success"] is True
        assert "000000000021" not in _read(sch)

    def test_handler_pin_not_found(self, iface, tmp_path):
        with patch("commands.pin_locator.PinLocator.get_pin_location", return_value=None):
            result = iface._handle_delete_schematic_no_connect(
                {"schematicPath": str(_write(tmp_path)), "componentRef": "U1", "pinNumber": "9"}
            )
        assert result["success"] is False
        assert "U1" in result["message"]

    def test_handler_no_flag_there(self, iface, tmp_path):
        result = iface._handle_delete_schematic_no_connect(
            {"schematicPath": str(_write(tmp_path)), "position": [1, 1]}
        )
        assert result["success"] is False

    def test_handler_needs_position_or_pin(self, iface, tmp_path):
        result = iface._handle_delete_schematic_no_connect({"schematicPath": str(_write(tmp_path))})
        assert result["success"] is False
        assert "componentRef" in result["message"]
