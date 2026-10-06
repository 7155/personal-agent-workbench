"""Contract checks for the narrow Squirrel system-tool fixtures."""

from __future__ import annotations

import json
import os
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from tests import squirrel_system_tools as tools


class SquirrelSystemToolsTests(unittest.TestCase):
    def test_unsupported_command_grammars_are_rejected(self) -> None:
        cases = (
            (tools.ditto, ["-c", "-k", "--unexpected", "source.app", "output.zip"]),
            (tools.plistbuddy, ["-unexpected", "Set :Key value", "Info.plist"]),
            (tools.sips, ["-s", "format", "tiff", "-z", "18", "18", "input.png", "--out", "output.png"]),
            (tools.iconutil, ["-c", "iconset", "source.icns", "-o", "output.iconset"]),
        )
        for adapter, args in cases:
            with self.subTest(tool=adapter.__name__):
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    adapter(args)

    def test_png_reader_rejects_corruption_truncation_and_bad_crc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.png"
            tools.write_png(path, 2, 2)
            original = path.read_bytes()
            bad_crc = bytearray(original)
            bad_crc[32] ^= 1
            cases = {
                "signature": b"X" + original[1:],
                "truncated": original[:-3],
                "crc": bytes(bad_crc),
            }
            for reason, payload in cases.items():
                with self.subTest(reason=reason):
                    path.write_bytes(payload)
                    with self.assertRaises(ValueError):
                        tools.read_png(path)

    def test_sips_resizes_real_rgba_pixels_with_valid_png_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "source.png", Path(tmp) / "output.png"
            pixels = bytes((255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 255, 255, 0, 128))
            tools.write_png(source, 2, 2, pixels)
            original = source.read_bytes()
            tools.sips(["-s", "format", "png", "-z", "6", "4", str(source), "--out", str(output)])
            width, height, actual = self._inspect_png(output.read_bytes())
            red, green, blue, yellow = (pixels[index : index + 4] for index in range(0, 16, 4))
            expected = (red * 2 + green * 2) * 3 + (blue * 2 + yellow * 2) * 3
            self.assertEqual((width, height, actual), (4, 6, expected))
            self.assertEqual(source.read_bytes(), original)

    def test_iconutil_emits_icns_with_valid_embedded_pngs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            iconset = self._make_iconset(Path(tmp))
            output = Path(tmp) / "icon.icns"
            tools.iconutil(["-c", "icns", str(iconset), "-o", str(output)])
            data = output.read_bytes()
            self.assertEqual(data[:4], b"icns")
            self.assertEqual(struct.unpack_from(">I", data, 4)[0], len(data))
            observed = {}
            offset = 8
            while offset < len(data):
                self.assertGreaterEqual(len(data) - offset, 8)
                kind, size = struct.unpack_from(">4sI", data, offset)
                self.assertGreaterEqual(size, 8)
                self.assertLessEqual(offset + size, len(data))
                self.assertNotIn(kind, observed)
                width, height, _ = self._inspect_png(data[offset + 8 : offset + size])
                observed[kind] = (width, height)
                offset += size
            self.assertEqual(offset, len(data))
            self.assertEqual(observed, {
                b"icp4": (16, 16), b"icp5": (32, 32), b"icp6": (64, 64),
                b"ic07": (128, 128), b"ic08": (256, 256),
                b"ic09": (512, 512), b"ic10": (1024, 1024),
            })

    def test_iconutil_rejects_missing_or_wrong_size_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            iconset = self._make_iconset(Path(tmp))
            member = iconset / "icon_16x16@2x.png"
            output = Path(tmp) / "icon.icns"
            for reason in ("missing", "wrong_size"):
                with self.subTest(reason=reason):
                    if reason == "missing":
                        member.unlink()
                    else:
                        tools.write_png(member, 16, 16)
                    with self.assertRaises(ValueError):
                        tools.iconutil(["-c", "icns", str(iconset), "-o", str(output)])
                    self.assertFalse(output.exists())

    def test_plist_mutations_preserve_other_fields_and_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Info.plist"
            sentinel = {"nested": ["keep", 17, b"bytes"]}
            for fmt in (plistlib.FMT_XML, plistlib.FMT_BINARY):
                with self.subTest(format=fmt):
                    path.write_bytes(plistlib.dumps({
                        "Sentinel": sentinel, "LSRegisterProhibited": True,
                        "CFBundleIconFile": "OldIcon",
                    }, fmt=fmt))
                    tools.plistbuddy(["-c", "Set :LSRegisterProhibited false", str(path)])
                    self.assertIs(plistlib.loads(path.read_bytes())["LSRegisterProhibited"], False)
                    for command in (
                        "Delete :LSRegisterProhibited",
                        "Set :CFBundleIconFile RagImeIcon",
                        "Add :FixtureEnabled bool true",
                        "Add :FixtureName string Fixture",
                    ):
                        tools.plistbuddy(["-c", command, str(path)])
                    data = path.read_bytes()
                    self.assertEqual(data.startswith(b"bplist00"), fmt == plistlib.FMT_BINARY)
                    self.assertEqual(plistlib.loads(data), {
                        "Sentinel": sentinel, "CFBundleIconFile": "RagImeIcon",
                        "FixtureEnabled": True, "FixtureName": "Fixture",
                    })

    def test_failed_plist_operations_do_not_change_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Info.plist"
            original = plistlib.dumps({"Existing": "sentinel"})
            for command in (
                "Set :Missing value", "Delete :Missing", "Add :Existing string overwrite",
                "Add :Invalid bool maybe", "Add :Nested:Key string value",
            ):
                with self.subTest(command=command):
                    path.write_bytes(original)
                    with self.assertRaises(ValueError):
                        tools.plistbuddy(["-c", command, str(path)])
                    self.assertEqual(path.read_bytes(), original)

    def test_ditto_cli_preserves_parent_contents_and_logs_exact_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Legacy Input.app"
            executable = source / "Contents" / "MacOS" / "Squirrel"
            executable.parent.mkdir(parents=True)
            sentinel = b"preserve executable\x00bytes\n"
            executable.write_bytes(sentinel)
            (source / "Contents" / "Empty").mkdir()
            output, log = root / "archive.zip", root / "tool-log.jsonl"
            args = ["-c", "-k", "--keepParent", str(source), str(output)]
            result = subprocess.run(
                [sys.executable, str(Path(tools.__file__).resolve()), "ditto", *args],
                env={**os.environ, "FAKE_SYSTEM_TOOL_LOG": str(log)},
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([json.loads(line) for line in log.read_text().splitlines()], [{"tool": "ditto", "args": args}])
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()), {
                    "Legacy Input.app/", "Legacy Input.app/Contents/",
                    "Legacy Input.app/Contents/MacOS/", "Legacy Input.app/Contents/MacOS/Squirrel",
                    "Legacy Input.app/Contents/Empty/",
                })
                self.assertEqual(archive.read("Legacy Input.app/Contents/MacOS/Squirrel"), sentinel)
            self.assertEqual(executable.read_bytes(), sentinel)

    def _make_iconset(self, root: Path) -> Path:
        iconset = root / "Fixture.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for suffix, scale in (("", 1), ("@2x", 2)):
                tools.write_png(iconset / f"icon_{size}x{size}{suffix}.png", size * scale, size * scale)
        return iconset

    def _inspect_png(self, data: bytes) -> tuple[int, int, bytes]:
        """Inspect generated files independently of the adapter's PNG reader."""
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        chunks = []
        offset = 8
        while offset < len(data):
            self.assertGreaterEqual(len(data) - offset, 12)
            length, kind = struct.unpack_from(">I4s", data, offset)
            end = offset + length + 12
            self.assertLessEqual(end, len(data))
            payload = data[offset + 8 : end - 4]
            self.assertEqual(struct.unpack_from(">I", data, end - 4)[0], zlib.crc32(kind + payload))
            chunks.append((kind, payload))
            offset = end
        self.assertEqual([kind for kind, _ in chunks], [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(chunks[-1][1], b"")
        width, height, *properties = struct.unpack(">IIBBBBB", chunks[0][1])
        self.assertEqual(properties, [8, 6, 0, 0, 0])
        decoder = zlib.decompressobj()
        rows = decoder.decompress(chunks[1][1]) + decoder.flush()
        self.assertTrue(decoder.eof)
        self.assertEqual(decoder.unused_data, b"")
        stride = width * 4 + 1
        self.assertEqual(len(rows), height * stride)
        self.assertTrue(all(rows[index * stride] == 0 for index in range(height)))
        pixels = b"".join(rows[index * stride + 1 : (index + 1) * stride] for index in range(height))
        return width, height, pixels


if __name__ == "__main__":
    unittest.main()
