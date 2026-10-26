"""
Tests for set_schematic_component_flags, set_title_block, purge_unused_lib_symbols,
add/delete_schematic_junction and list_stale_junctions.

All edits are made in place on a KiCad 10 file saved by eeschema: the tests
check that only the targeted lines change, that uuids survive, and that CRLF
files stay CRLF.
"""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

_FIXTURE = (Path(__file__).parent / "fixtures" / "canonical_schematic.kicad_sch").read_text(
    encoding="utf-8"
)
_PIN_TOP = (138.43, 35.56)
_PIN_BOTTOM = (138.43, 43.18)


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.fixture()
def iface():
    return _make_iface()


def _with_items(*items: str, content: str = _FIXTURE) -> str:
    """The fixture with top-level *items* (one line each) placed before sheet_instances."""
    marker = "\t(sheet_instances"
    return content.replace(marker, "".join(f"\t{i}\n" for i in items) + marker, 1)


def _write(tmp_path, content: str = _FIXTURE, crlf: bool = False) -> Path:
    sch = tmp_path / "test.kicad_sch"
    sch.write_bytes((content.replace("\n", "\r\n") if crlf else content).encode("utf-8"))
    return sch


def _read(sch: Path) -> str:
    return sch.read_bytes().decode("utf-8")


def _placed_symbol() -> str:
    start = _FIXTURE.index("\t(symbol\n\t\t(lib_id")
    end = _FIXTURE.index("\t(sheet_instances")
    return _FIXTURE[start:end]


def _wire(x1, y1, x2, y2) -> str:
    return (
        f"(wire (pts (xy {x1} {y1}) (xy {x2} {y2})) (stroke (width 0) (type default)) "
        f'(uuid "{x1}-{y1}-{x2}-{y2}"))'
    )


def _junction(x, y, uid="j") -> str:
    return f'(junction (at {x} {y}) (diameter 0) (color 0 0 0 0) (uuid "{uid}"))'


@pytest.mark.unit
class TestComponentFlags:
    def test_changes_only_the_flag_lines(self, tmp_path):
        from commands.schematic_items import set_component_flags

        sch = _write(tmp_path)
        assert set_component_flags(sch, "C9", {"dnp": True, "in_bom": False}) == 1

        expected = _FIXTURE.replace(
            "\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(in_pos_files yes)\n\t\t(dnp no)\n",
            "\t\t(in_bom no)\n\t\t(on_board yes)\n\t\t(in_pos_files yes)\n\t\t(dnp yes)\n",
        )
        assert expected != _FIXTURE
        assert _read(sch) == expected

    def test_leaves_lib_symbols_alone(self, tmp_path):
        from commands.schematic_items import set_component_flags

        sch = _write(tmp_path)
        set_component_flags(sch, "C9", {"on_board": False, "exclude_from_sim": True})
        content = _read(sch)
        lib = content[: content.index("\t(symbol\n\t\t(lib_id")]
        assert lib == _FIXTURE[: _FIXTURE.index("\t(symbol\n\t\t(lib_id")]
        assert "\t\t(exclude_from_sim yes)\n" in content
        assert "\t\t(on_board no)\n" in content

    def test_every_unit_of_a_multi_unit_part(self, tmp_path):
        from commands.schematic_items import set_component_flags

        unit2 = _placed_symbol().replace("(unit 1)", "(unit 2)")
        sch = _write(tmp_path, _FIXTURE.replace("\t(sheet_instances", unit2 + "\t(sheet_instances"))
        assert set_component_flags(sch, "C9", {"dnp": True}) == 2
        assert _read(sch).count("(dnp yes)") == 2

    def test_missing_flags_go_where_kicad_writes_them(self, tmp_path):
        from commands.schematic_items import set_component_flags

        stripped = _FIXTURE.replace("\t\t(dnp no)\n", "").replace(
            "\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n", "\t\t(exclude_from_sim no)\n"
        )
        assert "(dnp" not in stripped and stripped.count("(in_bom") == 1
        sch = _write(tmp_path, stripped)
        set_component_flags(sch, "C9", {"dnp": True, "in_bom": False})
        assert (
            "\t\t(exclude_from_sim no)\n\t\t(in_bom no)\n\t\t(on_board yes)\n"
            "\t\t(in_pos_files yes)\n\t\t(dnp yes)\n\t\t(fields_autoplaced yes)\n"
        ) in _read(sch)

    def test_reference_must_match_exactly(self, tmp_path):
        from commands.schematic_items import set_component_flags

        with pytest.raises(ValueError, match="'C' not found"):
            set_component_flags(_write(tmp_path), "C", {"dnp": True})

    def test_keeps_crlf(self, tmp_path):
        from commands.schematic_items import set_component_flags

        sch = _write(tmp_path, crlf=True)
        set_component_flags(sch, "C9", {"dnp": True})
        raw = sch.read_bytes()
        assert b"(dnp yes)\r\n" in raw
        assert raw.count(b"\n") == raw.count(b"\r\n")

    def test_handler(self, iface, tmp_path):
        sch = _write(tmp_path)
        result = iface._handle_set_schematic_component_flags(
            {"schematicPath": str(sch), "reference": "C9", "dnp": True, "inBom": False}
        )
        assert result["success"] is True
        assert result["flags"] == {"dnp": True, "in_bom": False}
        assert "(dnp yes)" in _read(sch)

    def test_handler_needs_a_flag(self, iface, tmp_path):
        result = iface._handle_set_schematic_component_flags(
            {"schematicPath": str(_write(tmp_path)), "reference": "C9"}
        )
        assert result["success"] is False

    def test_handler_unknown_reference(self, iface, tmp_path):
        result = iface._handle_set_schematic_component_flags(
            {"schematicPath": str(_write(tmp_path)), "reference": "R1", "dnp": True}
        )
        assert result["success"] is False
        assert "R1" in result["message"]


@pytest.mark.unit
class TestTitleBlock:
    def test_inserted_after_paper(self, tmp_path):
        from commands.schematic_items import set_title_block

        sch = _write(tmp_path)
        set_title_block(sch, {"title": "Main", "rev": "B"}, {2: "second"})
        expected = _FIXTURE.replace(
            '\t(paper "A4")\n',
            '\t(paper "A4")\n\t(title_block\n\t\t(title "Main")\n\t\t(rev "B")\n'
            '\t\t(comment 2 "second")\n\t)\n',
        )
        assert _read(sch) == expected

    def test_merges_in_kicad_order(self, tmp_path):
        from commands.schematic_items import set_title_block

        existing = _FIXTURE.replace(
            '\t(paper "A4")\n',
            '\t(paper "A4")\n\t(title_block\n\t\t(title "Old")\n\t\t(company "ACME")\n'
            '\t\t(comment 9 "nine")\n\t)\n',
        )
        sch = _write(tmp_path, existing)
        result = set_title_block(sch, {"title": "New", "date": "2026-10-26"}, {1: "one"})

        assert (
            '\t(title_block\n\t\t(title "New")\n\t\t(date "2026-10-26")\n'
            '\t\t(company "ACME")\n\t\t(comment 1 "one")\n\t\t(comment 9 "nine")\n\t)\n'
        ) in _read(sch)
        assert result == {
            "title": "New",
            "date": "2026-10-26",
            "company": "ACME",
            "comments": {1: "one", 9: "nine"},
        }

    def test_empty_string_clears_and_empty_block_goes(self, tmp_path):
        from commands.schematic_items import set_title_block

        sch = _write(tmp_path)
        set_title_block(sch, {"title": "T", "company": "C"}, {})
        set_title_block(sch, {"company": ""}, {})
        assert "company" not in _read(sch)
        set_title_block(sch, {"title": ""}, {})
        assert _read(sch) == _FIXTURE

    def test_escapes_values(self, tmp_path):
        from commands.schematic_items import set_title_block

        sch = _write(tmp_path)
        set_title_block(sch, {"title": 'Say "hi" \\ bye'}, {})
        assert r'(title "Say \"hi\" \\ bye")' in _read(sch)
        assert set_title_block(sch, {}, {})["title"] == 'Say "hi" \\ bye'

    def test_keeps_crlf(self, tmp_path):
        from commands.schematic_items import set_title_block

        sch = _write(tmp_path, crlf=True)
        set_title_block(sch, {"title": "T"}, {3: "c"})
        raw = sch.read_bytes()
        assert b'\t(title_block\r\n\t\t(title "T")\r\n' in raw
        assert raw.count(b"\n") == raw.count(b"\r\n")

    def test_handler(self, iface, tmp_path):
        sch = _write(tmp_path)
        result = iface._handle_set_title_block(
            {"schematicPath": str(sch), "title": "Board", "comment4": "four"}
        )
        assert result["success"] is True
        assert result["titleBlock"] == {"title": "Board", "comments": {4: "four"}}

    def test_handler_nothing_to_change(self, iface, tmp_path):
        result = iface._handle_set_title_block({"schematicPath": str(_write(tmp_path))})
        assert result["success"] is False


def _lib_entry(name: str) -> str:
    start = _FIXTURE.index('\t\t(symbol "Device:C"\n')
    end = _FIXTURE.index("\n\t\t)\n", start) + len("\n\t\t)\n")
    return (
        _FIXTURE[start:end]
        .replace('"Device:C"', f'"{name}"')
        .replace('"C_0_1"', f'"{name.split(":")[-1]}_0_1"')
        .replace('"C_1_1"', f'"{name.split(":")[-1]}_1_1"')
    )


def _with_lib_entries(*names: str, content: str = _FIXTURE) -> str:
    marker = "\t)\n\t(symbol\n\t\t(lib_id"
    return content.replace(marker, "".join(_lib_entry(n) for n in names) + marker, 1)


@pytest.mark.unit
class TestPurgeLibSymbols:
    def test_drops_only_unused_entries(self, tmp_path):
        from commands.schematic_items import purge_unused_lib_symbols

        sch = _write(tmp_path, _with_lib_entries("Device:R", "Device:L"))
        assert purge_unused_lib_symbols(sch) == ["Device:R", "Device:L"]
        assert _read(sch) == _FIXTURE

    def test_lib_name_counts_as_use(self, tmp_path):
        from commands.schematic_items import purge_unused_lib_symbols

        content = _with_lib_entries("C_1").replace(
            '\t(symbol\n\t\t(lib_id "Device:C")',
            '\t(symbol\n\t\t(lib_name "C_1")\n\t\t(lib_id "Device:C")',
        )
        sch = _write(tmp_path, content)
        assert purge_unused_lib_symbols(sch) == ["Device:C"]
        assert '(symbol "C_1"' in _read(sch)

    def test_nothing_unused_leaves_file_alone(self, tmp_path):
        from commands.schematic_items import purge_unused_lib_symbols

        sch = _write(tmp_path)
        assert purge_unused_lib_symbols(sch) == []
        assert _read(sch) == _FIXTURE

    def test_empty_cache_written_as_kicad_does(self, tmp_path):
        from commands.schematic_items import purge_unused_lib_symbols

        no_symbols = _FIXTURE.replace(_placed_symbol(), "")
        sch = _write(tmp_path, no_symbols)
        assert purge_unused_lib_symbols(sch) == ["Device:C"]
        assert "\t(lib_symbols)\n" in _read(sch)
        sexpdata.loads(_read(sch))

    def test_keeps_crlf(self, tmp_path):
        from commands.schematic_items import purge_unused_lib_symbols

        sch = _write(tmp_path, _with_lib_entries("Device:R"), crlf=True)
        purge_unused_lib_symbols(sch)
        assert sch.read_bytes() == _FIXTURE.replace("\n", "\r\n").encode("utf-8")

    def test_handler(self, iface, tmp_path):
        sch = _write(tmp_path, _with_lib_entries("Device:R"))
        result = iface._handle_purge_unused_lib_symbols({"schematicPath": str(sch)})
        assert result["success"] is True
        assert result["purged"] == ["Device:R"]

    def test_delete_component_option(self, iface, tmp_path):
        sch = _write(tmp_path)
        result = iface._handle_delete_schematic_component(
            {"schematicPath": str(sch), "reference": "C9", "purgeUnusedLibSymbols": True}
        )
        assert result["success"] is True
        assert result["purgedLibSymbols"] == ["Device:C"]
        assert "Device:C" not in _read(sch)

    def test_delete_component_keeps_cache_by_default(self, iface, tmp_path):
        sch = _write(tmp_path)
        result = iface._handle_delete_schematic_component(
            {"schematicPath": str(sch), "reference": "C9"}
        )
        assert "purgedLibSymbols" not in result
        assert '(symbol "Device:C"' in _read(sch)


@pytest.mark.unit
class TestJunctions:
    def test_add_in_kicad_form(self, tmp_path):
        from commands.schematic_items import add_junction

        sch = _write(tmp_path)
        assert add_junction(sch, [12.7, 25.4]) is True
        content = _read(sch)
        assert "\t(junction\n\t\t(at 12.7 25.4)\n\t\t(diameter 0)\n\t\t(color 0 0 0 0)\n" in content
        assert content.index("(junction") < content.index("(sheet_instances")

    def test_add_twice_is_a_no_op(self, tmp_path):
        from commands.schematic_items import add_junction

        sch = _write(tmp_path)
        add_junction(sch, [12.7, 25.4])
        assert add_junction(sch, [12.7, 25.4]) is False
        assert _read(sch).count("(junction") == 1

    def test_add_keeps_crlf(self, tmp_path):
        from commands.schematic_items import add_junction

        sch = _write(tmp_path, crlf=True)
        add_junction(sch, [1, 2])
        raw = sch.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n")

    def test_delete(self, tmp_path):
        from commands.schematic_items import delete_items_at

        sch = _write(tmp_path, _with_items(_junction(10, 10, "a"), _junction(20, 10, "b")))
        assert delete_items_at(sch, "junction", [10, 10]) == 1
        content = _read(sch)
        assert '"a"' not in content and '"b"' in content
        assert content == _with_items(_junction(20, 10, "b"))

    def test_stale_junctions(self, tmp_path):
        from commands.schematic_items import stale_junctions

        px, py = _PIN_TOP
        content = _with_items(
            # T of three wire ends at (50, 50): fine
            _wire(40, 50, 50, 50),
            _wire(50, 50, 60, 50),
            _wire(50, 50, 50, 60),
            _junction(50, 50, "tee"),
            # a wire through (80, 50) and one ending on it: fine
            _wire(70, 50, 90, 50),
            _wire(80, 50, 80, 60),
            _junction(80, 50, "through"),
            # two wire ends only, after a third wire was deleted: stale
            _wire(100, 50, 110, 50),
            _wire(110, 50, 110, 60),
            _junction(110, 50, "corner"),
            # a pin and two wire ends: fine
            _wire(px - 10, py, px, py),
            _wire(px, py, px, py - 10),
            _junction(px, py, "pin"),
            # nothing at all: stale
            _junction(200, 200, "lonely"),
        )
        stale = stale_junctions(_write(tmp_path, content))
        assert {j["uuid"]: j["connections"] for j in stale} == {"corner": 2, "lonely": 0}

    def test_handlers(self, iface, tmp_path):
        sch = _write(tmp_path)
        added = iface._handle_add_schematic_junction(
            {"schematicPath": str(sch), "position": [30, 30]}
        )
        assert added["success"] is True and added["added"] is True

        listed = iface._handle_list_stale_junctions({"schematicPath": str(sch)})
        assert listed["count"] == 1

        deleted = iface._handle_delete_schematic_junction(
            {"schematicPath": str(sch), "position": [30, 30]}
        )
        assert deleted["success"] is True
        assert _read(sch) == _FIXTURE

        missing = iface._handle_delete_schematic_junction(
            {"schematicPath": str(sch), "position": [30, 30]}
        )
        assert missing["success"] is False

    def test_handler_needs_position(self, iface, tmp_path):
        result = iface._handle_add_schematic_junction({"schematicPath": str(_write(tmp_path))})
        assert result["success"] is False
