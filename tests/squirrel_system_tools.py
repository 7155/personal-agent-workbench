"""Narrow stdlib tool contracts for the Squirrel install orchestration fixture.

Run as ``python adapter.py TOOL args...``. These adapters exercise real zip,
plist, PNG and ICNS artifacts, not native macOS rendering or metadata fidelity.
PNG input is deliberately limited to the unfiltered RGBA fixtures made here.
Unknown commands and formats fail instead of silently simulating success.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import struct
import sys
import zipfile
import zlib
from pathlib import Path


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def write_png(path: Path, width: int, height: int, pixels: bytes | None = None) -> None:
    """Write valid, filter-zero RGBA PNG; default pixels form a tiny color grid."""
    require(1 <= width <= 1024 and 1 <= height <= 1024, "unsupported PNG dimensions")
    if pixels is None:
        pixels = bytes(
            component
            for y in range(height)
            for x in range(width)
            for component in (255 * (x % 2), 255 * (y % 2), 127, 255)
        )
    require(len(pixels) == width * height * 4, "invalid RGBA payload length")
    stride = width * 4
    scanlines = b"".join(
        b"\0" + pixels[y * stride : (y + 1) * stride] for y in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    Path(path).write_bytes(
        PNG_SIGNATURE
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(scanlines))
        + png_chunk(b"IEND", b"")
    )


def read_png(path: Path) -> tuple[int, int, bytes]:
    """Validate our narrow fixture PNG format and return width, height, RGBA."""
    data = Path(path).read_bytes()
    require(data.startswith(PNG_SIGNATURE), "invalid PNG signature")
    chunks: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset < len(data):
        require(offset + 12 <= len(data), "truncated PNG chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        require(end <= len(data), "truncated PNG chunk payload")
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        require(crc == zlib.crc32(kind + payload), "invalid PNG CRC")
        chunks.append((kind, payload))
        offset = end
    require([kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"], "unsupported PNG chunks")
    require(len(chunks[0][1]) == 13 and chunks[2][1] == b"", "invalid PNG framing")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", chunks[0][1]
    )
    require((depth, color, compression, filtering, interlace) == (8, 6, 0, 0, 0), "unsupported PNG format")
    require(1 <= width <= 1024 and 1 <= height <= 1024, "unsupported PNG dimensions")
    expected_size = height * (width * 4 + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(chunks[1][1], expected_size + 1)
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, "invalid PNG compression stream")
    require(len(raw) == expected_size, "invalid PNG pixel length")
    stride = width * 4 + 1
    require(all(raw[y * stride] == 0 for y in range(height)), "unsupported PNG row filter")
    pixels = b"".join(raw[y * stride + 1 : (y + 1) * stride] for y in range(height))
    return width, height, pixels


def ditto(args: list[str]) -> None:
    require(len(args) == 5 and args[:3] == ["-c", "-k", "--keepParent"], "unsupported ditto arguments")
    source, output = map(Path, args[3:])
    require(source.is_dir() and source.suffix == ".app", "ditto source must be an app directory")
    require(not output.exists() and output.parent.is_dir(), "ditto requires a new archive in an existing directory")
    require(not output.resolve().is_relative_to(source.resolve()), "archive must be outside source")
    members = [source, *sorted(source.rglob("*"))]
    require(all(not item.is_symlink() and (item.is_dir() or item.is_file()) for item in members), "unsupported archive member type")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in members:
            archive.write(item, item.relative_to(source.parent).as_posix())
    with zipfile.ZipFile(output) as archive:
        require(archive.testzip() is None, "generated archive failed CRC validation")
        for item in members:
            if item.is_file():
                require(archive.read(item.relative_to(source.parent).as_posix()) == item.read_bytes(), "archive content differs from source")


def plistbuddy(args: list[str]) -> None:
    require(len(args) == 3 and args[0] == "-c", "unsupported PlistBuddy arguments")
    command = shlex.split(args[1])
    require(len(command) >= 2, "missing PlistBuddy command or field")
    operation, field = command[:2]
    require(field.startswith(":") and len(field) > 1 and ":" not in field[1:], "only flat plist fields are supported")
    key = field[1:]
    path = Path(args[2])
    source = path.read_bytes()
    payload = plistlib.loads(source)
    require(isinstance(payload, dict), "plist root must be a dictionary")
    if operation == "Delete":
        require(len(command) == 2 and key in payload, "Delete requires an existing field")
        del payload[key]
    elif operation == "Set":
        require(len(command) == 3 and key in payload, "Set requires an existing field and one value")
        current = payload[key]
        if type(current) is bool:
            require(command[2] in {"true", "false"}, "invalid bool value")
            payload[key] = command[2] == "true"
        else:
            require(type(current) is str, "only bool/string Set is supported")
            payload[key] = command[2]
    elif operation == "Add":
        require(len(command) == 4 and key not in payload, "Add requires a new field, type and value")
        kind, value = command[2:]
        require(kind in {"bool", "string"}, "only bool/string Add is supported")
        if kind == "bool":
            require(value in {"true", "false"}, "invalid bool value")
            payload[key] = value == "true"
        else:
            payload[key] = value
    else:
        raise ValueError(f"unsupported PlistBuddy operation: {operation}")
    fmt = plistlib.FMT_BINARY if source.startswith(b"bplist00") else plistlib.FMT_XML
    path.write_bytes(plistlib.dumps(payload, fmt=fmt, sort_keys=False))
    require(plistlib.loads(path.read_bytes()) == payload, "generated plist failed validation")


def sips(args: list[str]) -> None:
    require(len(args) == 9 and args[:4] == ["-s", "format", "png", "-z"] and args[7] == "--out", "unsupported sips arguments")
    height, width = int(args[4]), int(args[5])
    require(1 <= width <= 1024 and 1 <= height <= 1024, "unsupported resize dimensions")
    source, output = Path(args[6]), Path(args[8])
    require(source.resolve() != output.resolve(), "in-place resizing is unsupported")
    source_width, source_height, pixels = read_png(source)
    resized = b"".join(
        pixels[offset : offset + 4]
        for y in range(height)
        for x in range(width)
        for offset in [((y * source_height // height) * source_width + x * source_width // width) * 4]
    )
    write_png(output, width, height, resized)
    require(read_png(output) == (width, height, resized), "generated PNG failed validation")


def iconutil(args: list[str]) -> None:
    require(len(args) == 5 and args[:2] == ["-c", "icns"] and args[3] == "-o", "unsupported iconutil arguments")
    iconset, output = Path(args[2]), Path(args[4])
    require(iconset.is_dir() and iconset.suffix == ".iconset", "invalid iconset directory")
    expected = {
        f"icon_{size}x{size}{suffix}.png": size * scale
        for size in (16, 32, 128, 256, 512)
        for suffix, scale in (("", 1), ("@2x", 2))
    }
    require({path.name for path in iconset.iterdir()} == set(expected), "unexpected iconset contents")
    for name, size in expected.items():
        width, height, _ = read_png(iconset / name)
        require((width, height) == (size, size), f"incorrect icon size: {name}")
    selected = (
        (b"icp4", "icon_16x16.png"),
        (b"icp5", "icon_32x32.png"),
        (b"icp6", "icon_32x32@2x.png"),
        (b"ic07", "icon_128x128.png"),
        (b"ic08", "icon_256x256.png"),
        (b"ic09", "icon_512x512.png"),
        (b"ic10", "icon_512x512@2x.png"),
    )
    chunks = []
    for kind, name in selected:
        png = (iconset / name).read_bytes()
        chunks.append(kind + struct.pack(">I", len(png) + 8) + png)
    body = b"".join(chunks)
    result = b"icns" + struct.pack(">I", len(body) + 8) + body
    output.write_bytes(result)
    require(output.read_bytes() == result, "generated ICNS failed validation")


def main() -> int:
    try:
        require(len(sys.argv) >= 2, "usage: adapter.py TOOL args...")
        tool, args = sys.argv[1], sys.argv[2:]
        log = os.environ.get("FAKE_SYSTEM_TOOL_LOG")
        if log:
            with Path(log).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"tool": tool, "args": args}) + "\n")
        dispatch = {"ditto": ditto, "PlistBuddy": plistbuddy, "sips": sips, "iconutil": iconutil}
        require(tool in dispatch, f"unsupported fixture tool: {tool}")
        dispatch[tool](args)
    except (OSError, ValueError, OverflowError, struct.error, zlib.error, plistlib.InvalidFileException, zipfile.BadZipFile) as exc:
        print(f"fixture tool error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
