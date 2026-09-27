#!/usr/bin/env python3
"""Validate an offline payload, then enter the existing product stack installer.

The DMG copies its payload into a private persistent generation first. Runtime
paths therefore survive ejecting the disk image. This module does not own any
service lifecycle: install_product_stack.sh and its existing helpers do.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def inventory(root: Path) -> dict[str, dict[str, str]]:
    result = {}
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if name == 'installer-manifest.json':
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f'payload symlink escapes its root: {name}')
            result[name] = {'link': os.readlink(path)}
        elif path.is_file():
            with path.open('rb') as stream:
                result[name] = {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}
    return result


def verify(root: Path) -> dict:
    manifest = json.loads((root / 'installer-manifest.json').read_text())
    if manifest.get('schemaVersion') != 'paw.binary-installer.v1':
        raise ValueError('unsupported binary installer manifest')
    if not re.fullmatch('[0-9a-f]{40}', manifest.get('productSourceCommit', '')):
        raise ValueError('missing product source revision')
    expected = manifest.get('files')
    if not isinstance(expected, dict) or not expected:
        raise ValueError('empty payload inventory')
    # Runtime canaries write receipts beside pi-runtime, never into a hashed
    # component. Compare the complete shipped inventory, including symlinks.
    actual = inventory(root)
    for name, digest in expected.items():
        if actual.get(name) != digest:
            raise ValueError(f'payload content differs: {name}')
    unexpected = set(actual) - set(expected)
    unexpected = {name for name in unexpected if not re.fullmatch(r'pi-runtime\.[a-z-]+\.json', name)}
    if unexpected:
        raise ValueError(f'unexpected payload content: {sorted(unexpected)[0]}')
    required = (
        'python/bin/python3', 'source/scripts/install_product_stack.sh',
        'apps/RagImeControlElectron.app/Contents/MacOS/RagImeControl',
        'apps/RagImeVoice.app/Contents/MacOS/RagImeVoice',
        'apps/RagImeDesktopBridge.app/Contents/MacOS/RagImeDesktopBridge',
        'pi-runtime/manifest.json',
        'source/integrations/ego-browser/upstream/paw-ego-browser-runtime.json',
        'source/integrations/ego-browser/upstream/package/ego-browser/dist/src/run.js',
        'source/integrations/ego-browser/upstream/package/ego-linux-host/dist/cli.js',
    )
    for name in required:
        if name not in expected or not (root / name).is_file():
            raise ValueError(f'missing required component: {name}')
    for app in ('RagImeControlElectron.app', 'RagImeVoice.app', 'RagImeDesktopBridge.app'):
        if sys.platform == 'darwin':
            subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(root / 'apps' / app)], check=True)
    return manifest


def snapshot(home: Path, support: Path, target: Path) -> None:
    target.mkdir(mode=0o700)
    paths = [home / 'Applications' / name for name in (
        'Personal Agent Workbench.app', 'RagImeControl.app',
        'RagImeVoice.app', 'RagImeDesktopBridge.app')]
    paths += [support / 'app', support / 'PiRuntime/current.json']
    paths += list((home / 'Library/LaunchAgents').glob('com.rag-ime.*.plist'))
    copied = []
    for number, source in enumerate(paths):
        if not source.exists() and not source.is_symlink():
            continue
        dest = target / f'{number:02d}-{source.name}'
        if source.is_symlink():
            dest.symlink_to(os.readlink(source))
        elif source.is_dir():
            subprocess.run(['/usr/bin/ditto', str(source), str(dest)], check=True)
        else:
            shutil.copy2(source, dest)
        copied.append({'original': str(source), 'backup': str(dest)})
    (target / 'recovery.json').write_text(json.dumps({
        'files': copied, 'userDatabasesAndCredentials': 'retained in place; never packaged',
    }, indent=2) + '\n')


def install(root: Path) -> int:
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('This preview requires an Apple Silicon Mac.')
    if int(platform.mac_ver()[0].split('.')[0]) < 14:
        raise ValueError('This preview requires macOS 14 or later.')
    home = Path.home()
    support = home / 'Library/Application Support/RagIme'
    support.mkdir(parents=True, exist_ok=True)
    with (support / '.binary-install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another PAW installation is already running.') from None
        manifest = verify(root)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        recovery = support / 'InstallerRecovery' / stamp
        recovery.parent.mkdir(mode=0o700, exist_ok=True)
        print(f'Backing up installed components to {recovery}', flush=True)
        snapshot(home, support, recovery)
        python = root / 'python/bin/python3'
        env = {**os.environ,
            'PATH': f'{root / "python/bin"}:/usr/bin:/bin:/usr/sbin:/sbin',
            'PYTHONDONTWRITEBYTECODE': '1',
            'PAW_BINARY_PAYLOAD': str(root),
            'RAG_IME_APP_SUPPORT_DIR': str(support),
            'RAG_IME_PYTHON': str(python),
        }
        env.pop('PYTHONHOME', None)
        env.pop('PYTHONPATH', None)
        # An existing optional MLX installation may own a larger Python runtime.
        # Preserve that configuration on updates; fresh installs use bundled
        # Python and do not download optional model weights.
        sidecar = home / 'Library/LaunchAgents/com.rag-ime.sidecar.plist'
        if sidecar.exists():
            config = plistlib.loads(sidecar.read_bytes()).get('EnvironmentVariables', {})
            if config.get('RAG_IME_EMBEDDING_PROVIDER', '').lower() in ('mlx-bert', 'local-bge-mlx', 'mlx-bge'):
                existing = config.get('RAG_IME_KNOWLEDGE_PYTHON', '')
                if existing and Path(existing).is_file():
                    env['RAG_IME_PYTHON'] = existing
        else:
            env['RAG_IME_SIDECAR_NO_SEED'] = '1'
        print('Installing desktop, Pi Runtime, Sidecar, voice and desktop bridge…', flush=True)
        result = subprocess.run(['/bin/bash', str(root / 'source/scripts/install_product_stack.sh'),
                                 '--binary-payload', str(root), '--skip-mlx'], env=env)
        receipt = {'productSourceCommit': manifest['productSourceCommit'],
                   'installerSourceCommit': manifest['installerSourceCommit'],
                   'payload': str(root), 'recovery': str(recovery),
                   'exitCode': result.returncode, 'completedAt': datetime.now(timezone.utc).isoformat()}
        (recovery / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
        if result.returncode:
            print(f'Installation stopped. Recovery files: {recovery}', flush=True)
        else:
            print('Installation complete. Open Personal Agent Workbench from ~/Applications.', flush=True)
        return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--verify', type=Path)
    group.add_argument('--install', type=Path)
    args = parser.parse_args()
    if args.verify:
        manifest = verify(args.verify.resolve())
        print(f'Verified offline payload: {manifest["productVersion"]}')
        return 0
    return install(args.install.resolve())


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
