"""New files carry the installed KiCad's own format versions.

KiCad refuses a file that declares a newer format than it knows, and the format
moves even within a release series (10.0.x: 20260101 -> 20260306), so the versions
come from the installed kicad-cli (cached per binary), else from KNOWN by major.
Hidden fields are written the way the file's KiCad version writes them.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

pytestmark = pytest.mark.unit

from commands.schematic_text_utils import _apply_visibility  # noqa: E402

from utils import kicad_format  # noqa: E402

PROBED = {"sch": 20260306, "sym": 20251024, "fp": 20260206, "generator_version": "10.0"}


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    kicad_format.formats.cache_clear()
    monkeypatch.setattr(kicad_format, "_cache_file", lambda: tmp_path / "formats.json")
    yield
    kicad_format.formats.cache_clear()


def test_versions_come_from_kicad_cli_once_per_binary(fresh, monkeypatch, tmp_path):
    cli = tmp_path / "kicad-cli"
    cli.write_text("")
    probes = []
    monkeypatch.setattr(kicad_format, "resolve_kicad_cli", lambda: str(cli))
    monkeypatch.setattr(kicad_format, "_probe", lambda c: probes.append(c) or dict(PROBED))
    assert kicad_format.formats() == PROBED
    kicad_format.formats.cache_clear()
    assert kicad_format.formats() == PROBED
    assert probes == [str(cli)]


def test_a_different_kicad_cli_is_probed_again(fresh, monkeypatch, tmp_path):
    probes = []
    monkeypatch.setattr(kicad_format, "_probe", lambda c: probes.append(c) or dict(PROBED))
    for name in ("a", "b"):
        cli = tmp_path / name
        cli.write_text(name)
        monkeypatch.setattr(kicad_format, "resolve_kicad_cli", lambda c=cli: str(c))
        kicad_format.formats.cache_clear()
        kicad_format.formats()
    assert len(probes) == 2


@pytest.mark.parametrize("version,major", [("9.0.4", 9), ("10.0.7", 10), ("11.0.0", 10)])
def test_without_kicad_cli_the_table_follows_pcbnew(fresh, monkeypatch, version, major):
    monkeypatch.setattr(kicad_format, "resolve_kicad_cli", lambda: None)
    monkeypatch.setitem(
        sys.modules, "pcbnew", type("pcbnew", (), {"Version": staticmethod(lambda: version)})
    )
    assert kicad_format.formats() == kicad_format.KNOWN[major]


def test_templates_are_stamped(fresh, monkeypatch):
    monkeypatch.setattr(kicad_format, "_from_cli", lambda: dict(PROBED))
    old = '(kicad_sch (version 20250114) (generator "KiCAD-MCP-Server")\n  (uuid 1)\n'
    assert kicad_format.stamp_schematic(old).startswith(
        '(kicad_sch (version 20260306) (generator "KiCAD-MCP-Server") (generator_version "10.0")'
    )
    v10 = '(kicad_sch (version 20260101) (generator "eeschema") (generator_version "10.0")\n'
    assert kicad_format.stamp_schematic(v10).startswith("(kicad_sch (version 20260306)")


def test_empty_symbol_lib(fresh, monkeypatch):
    monkeypatch.setattr(kicad_format, "_from_cli", lambda: dict(PROBED))
    lib = kicad_format.empty_symbol_lib()
    assert "(version 20251024)" in lib and '(generator_version "10.0")' in lib


V10_BLOCK = (
    '(symbol (lib_id "Device:R") (at 50 50 0) (unit 1)\n'
    "\t\t(in_pos_files yes)\n"
    '\t\t(property "Value" "10k"\n'
    "\t\t\t(at 52 53 0)\n"
    "\t\t\t(show_name no)\n"
    "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n"
    "\t\t)\n)"
)
V9_BLOCK = (
    '(symbol (lib_id "Device:R") (at 50 50 0) (unit 1)\n'
    '    (property "Value" "10k" (at 52 53 0)\n'
    "      (effects (font (size 1.27 1.27)))\n    )\n)"
)


def test_kicad10_hides_with_a_top_level_flag():
    hidden = _apply_visibility(V10_BLOCK, "Value", False)
    assert "\t\t\t(at 52 53 0)\n\t\t\t(hide yes)\n\t\t\t(show_name no)" in hidden
    assert _apply_visibility(hidden, "Value", True) == V10_BLOCK


def test_older_files_hide_inside_effects():
    hidden = _apply_visibility(V9_BLOCK, "Value", False)
    assert "(effects (font (size 1.27 1.27)) (hide yes))" in hidden
    assert _apply_visibility(hidden, "Value", True) == V9_BLOCK


def test_any_old_flag_is_replaced():
    bare = V9_BLOCK.replace("(size 1.27 1.27)))", "(size 1.27 1.27)) hide)")
    assert _apply_visibility(bare, "Value", True) == V9_BLOCK
    effects_flag = V10_BLOCK.replace("\t\t\t)\n\t\t)", "\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)")
    assert "(hide yes)" not in _apply_visibility(effects_flag, "Value", True)
