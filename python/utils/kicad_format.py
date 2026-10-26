"""The file-format versions to write: the ones the installed KiCad writes itself.

A KiCad build refuses a file that declares a newer format than it knows, and the
schematic format moved within 10.0.x (20260101 -> 20260306), so any hard-coded
version is stale for some installs and too new for others. Instead the installed
kicad-cli upgrades a minimal schematic, symbol library and footprint, and the
versions it writes are what this server stamps on new files. That takes a few
seconds, so the answer is cached on disk per kicad-cli binary.

Without kicad-cli, KNOWN stands in, by the KiCad major version pcbnew reports.
"""

import functools
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional, Union

from utils.kicad_cli import resolve_kicad_cli

logger = logging.getLogger("kicad_interface")

Formats = Dict[str, Union[int, str]]

KNOWN: Dict[int, Formats] = {
    9: {"sch": 20250114, "sym": 20241209, "fp": 20241229, "generator_version": "9.0"},
    10: {"sch": 20260306, "sym": 20251024, "fp": 20260206, "generator_version": "10.0"},
}

_PROBES = {
    "sch": ("p.kicad_sch", '(kicad_sch (version 20231120) (generator "eeschema"))\n'),
    "sym": (
        "p.kicad_sym",
        '(kicad_symbol_lib (version 20211014) (generator "kicad_symbol_editor"))\n',
    ),
    "fp": (
        "p.pretty/p.kicad_mod",
        '(footprint "p" (version 20211014) (generator "pcbnew") (layer "F.Cu"))\n',
    ),
}


def _cache_file() -> Path:
    base = (
        os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    )
    return Path(base) / "kicad-mcp" / "formats.json"


def _probe(cli: str) -> Optional[Formats]:
    out: Formats = {}
    with tempfile.TemporaryDirectory() as d:
        for key, (name, text) in _PROBES.items():
            path = Path(d) / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
            target = path.parent if key == "fp" else path
            subprocess.run(
                [cli, key, "upgrade", "--force", str(target)],
                capture_output=True,
                timeout=60,
                check=True,
            )
            m = re.search(r"\(version\s+(\d+)\)", path.read_text(encoding="utf-8"))
            if not m:
                return None
            out[key] = int(m.group(1))
        version = subprocess.run(
            [cli, "version"], capture_output=True, text=True, timeout=60, check=True
        ).stdout.strip()
    major_minor = re.match(r"(\d+)\.(\d+)", version)
    if not major_minor:
        return None
    out["generator_version"] = f"{major_minor.group(1)}.{major_minor.group(2)}"
    return out


def _from_cli() -> Optional[Formats]:
    cli = resolve_kicad_cli()
    if not cli:
        return None
    stat = Path(cli).stat()
    key = f"{cli}|{stat.st_size}|{int(stat.st_mtime)}"
    cache = _cache_file()
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("key") == key:
            return cached["formats"]
    except (OSError, ValueError, KeyError):
        pass
    found = _probe(cli)
    if found:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"key": key, "formats": found}), encoding="utf-8")
        except OSError as e:
            logger.warning(f"Could not cache KiCad file formats in {cache}: {e}")
    return found


def _major_from_pcbnew() -> int:
    try:
        import pcbnew

        return int(str(pcbnew.Version()).split(".")[0])
    except Exception:
        return max(KNOWN)


@functools.lru_cache(maxsize=None)
def formats() -> Formats:
    """{"sch", "sym", "fp": format version, "generator_version": "10.0"} for new files."""
    try:
        found = _from_cli()
        if found:
            return found
    except Exception as e:
        logger.warning(f"kicad-cli did not report its file formats ({e}); using the table")
    major = _major_from_pcbnew()
    return KNOWN[min(max(major, min(KNOWN)), max(KNOWN))]


def schematic_header() -> str:
    """The opening of a new .kicad_sch, as eeschema writes it."""
    f = formats()
    return (
        f'(kicad_sch (version {f["sch"]}) (generator "eeschema")'
        f' (generator_version "{f["generator_version"]}")\n\n'
    )


def empty_symbol_lib() -> str:
    """A new, empty .kicad_sym in the installed KiCad's format."""
    f = formats()
    return (
        "(kicad_symbol_lib\n"
        f"\t(version {f['sym']})\n"
        '\t(generator "kicad-mcp")\n'
        f'\t(generator_version "{f["generator_version"]}")\n'
        ")\n"
    )


def stamp_schematic(content: str) -> str:
    """Give a schematic written from a template the installed KiCad's header."""
    f = formats()
    content = re.sub(r"\(version\s+\d+\)", f"(version {f['sch']})", content, count=1)
    if "(generator_version" in content[:300]:
        return re.sub(
            r'\(generator_version\s+"[^"]*"\)',
            f'(generator_version "{f["generator_version"]}")',
            content,
            count=1,
        )
    return re.sub(
        r'(\(generator\s+"[^"]*"\))',
        rf'\1 (generator_version "{f["generator_version"]}")',
        content,
        count=1,
    )
