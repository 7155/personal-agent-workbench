#!/usr/bin/env python3
"""Assemble an unsigned offline DMG from verified, already built components."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from install_binary_payload import inventory, verify

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_FILES = (
    'scripts/install_product_stack.sh', 'scripts/install_binary_payload.py',
    'scripts/build_control_center_web.sh', 'scripts/build_paw_os_electron_host.sh',
    'scripts/archive_paw_app_backups.py',
    'scripts/build_voice_input.sh', 'scripts/build_desktop_bridge.sh',
    'scripts/install_sidecar_launch_agent.sh',
    'scripts/build_ego_browser_runtime.py',
    'scripts/install_memory_book_maintenance_launch_agent.sh',
    'scripts/support/prebuilt_product.sh',
)


def run(*args: str | Path) -> None:
    subprocess.run([str(arg) for arg in args], check=True)


def git(*args: str | Path, cwd: Path = ROOT) -> str:
    return subprocess.check_output(['git', '-C', str(cwd), *map(str, args)], text=True).strip()


def archive(repo: Path, revision: str, target: Path) -> None:
    run('git', '-C', repo, 'archive', '--format=tar.gz', '-o', target, revision)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apps', type=Path, required=True, help='Directory containing the three built apps')
    parser.add_argument('--pi-payload', type=Path, required=True)
    parser.add_argument('--pi-source', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True, help='Portable CPython install with runtime dependencies')
    parser.add_argument('--electron-notices', type=Path, required=True, help='Pinned electron/dist directory')
    parser.add_argument('--output', type=Path, required=True, help='New output directory')
    args = parser.parse_args()
    if git('status', '--porcelain', '--untracked-files=no'):
        raise SystemExit('Commit installer changes before creating a distributable.')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    volume = output / 'volume'
    app = volume / 'Install Personal Agent Workbench.app'
    contents = app / 'Contents'
    resources = contents / 'Resources'
    payload = resources / 'payload'
    payload.mkdir(parents=True)
    control = args.apps / 'RagImeControlElectron.app'
    marker = json.loads((control / 'Contents/Resources/rag-ime-control-web-build-marker.json').read_text())
    commit = marker['sourceCommit']
    version = marker['productVersion']
    if marker.get('sourceDirty') is not False:
        raise SystemExit('Cannot distribute a dirty app build.')
    source = payload / 'source'
    source.mkdir()
    licenses = payload / 'corresponding-source'
    licenses.mkdir()
    product_archive = licenses / f'paw-{commit[:12]}.tar.gz'
    archive(ROOT, commit, product_archive)
    with tarfile.open(product_archive) as bundle:
        bundle.extractall(source, filter='data')
    installer_commit = git('rev-parse', 'HEAD')
    archive(ROOT, installer_commit, licenses / f'paw-installer-{installer_commit[:12]}.tar.gz')
    for name in INSTALLER_FILES:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, source / name)
    for name in ('RagImeControlElectron.app', 'RagImeVoice.app', 'RagImeDesktopBridge.app'):
        run('/usr/bin/ditto', args.apps / name, payload / 'apps' / name)
    for app_name, marker_name in (('RagImeVoice.app', 'rag-ime-voice-build-marker.json'),
                                 ('RagImeDesktopBridge.app', 'rag-ime-desktop-bridge-build-marker.json')):
        native = json.loads((payload / 'apps' / app_name / 'Contents/Resources' / marker_name).read_text())
        if native.get('gitCommit') != commit or native.get('gitDirty') is not False:
            raise SystemExit(f'Native component differs from product source: {app_name}')
    run('/usr/bin/ditto', control / 'Contents/Resources/app/dist', source / 'control-center-web/dist')
    # App preparation on a binary-only installation must not require Node or
    # a fresh frontend build. Ship the same compiled shared controls as source.
    portable_ui = ROOT / 'control-center-web/.generated/portable-agent-ui'
    if not all((portable_ui / name).is_file() for name in ('agent-ui.js', 'agent-ui.css')):
        raise SystemExit('Build portable Agent UI controls before packaging the installer.')
    run('/usr/bin/ditto', portable_ui, source / 'control-center-web/.generated/portable-agent-ui')
    if git('rev-parse', 'HEAD') != commit:
        raise SystemExit('Browser runtime must be packaged from the desktop product source revision.')
    run(sys.executable, ROOT / 'scripts/build_ego_browser_runtime.py',
        '--output', source / 'integrations/ego-browser/upstream', '--replace')
    run('/usr/bin/ditto', args.pi_payload, payload / 'pi-runtime')
    pi_manifest_path = payload / 'pi-runtime/manifest.json'
    pi = json.loads(pi_manifest_path.read_text())
    if pi['source']['productCommit'] != commit:
        raise SystemExit('Pi payload and desktop product revisions differ.')
    pi_commit = pi['source']['commit']
    archive(args.pi_source, pi_commit, licenses / f'pi-{pi_commit[:12]}.tar.gz')
    notices = payload / 'third-party-notices'
    notices.mkdir()
    for name in ('LICENSE', 'LICENSES.chromium.html'):
        shutil.copy2(args.electron_notices / name, notices / f'Electron-{name}')
    node_version = subprocess.check_output([str(payload / 'pi-runtime/bin/node'), '--version'], text=True).strip()
    with urllib.request.urlopen(f'https://raw.githubusercontent.com/nodejs/node/{node_version}/LICENSE', timeout=30) as response:
        (notices / 'Node-LICENSE.txt').write_bytes(response.read())
    for path in (args.pi_source / 'node_modules').rglob('*'):
        if path.is_file() and path.name.lower().startswith(('license', 'licence', 'copying', 'notice')):
            target = notices / 'pi-dependencies' / path.relative_to(args.pi_source / 'node_modules')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    # Public metadata must not expose the packager's local checkout path.
    pi['source']['productRepository'] = 'https://github.com/7155/personal-agent-workbench.git'
    # A UI-only product update can reuse identical Pi code. Give its changed
    # product provenance a separate generation rather than colliding with the
    # previously accepted manifest at the same runtime directory.
    pi['runtimeVersion'] += f'-offline-{commit[:8]}'
    pi_manifest_path.write_text(json.dumps(pi, indent=2) + '\n')
    run('/usr/bin/ditto', args.python, payload / 'python')
    # These development convenience scripts carry the build machine's Python
    # shebang. The installed runtime only needs the relocatable interpreter.
    for executable in (payload / 'python/bin').iterdir():
        if not executable.name.startswith('python') or executable.name.endswith('-config'):
            executable.unlink()
    # Python cache files are machine-generated, not distributable source.
    for cache in list(payload.rglob('__pycache__')):
        shutil.rmtree(cache)
    run(payload / 'python/bin/python3', '-B', '-c',
        'import sys, sqlite3, ssl, pypdf, yaml, jsonschema, certifi; assert sys.version_info >= (3, 12)')
    manifest = {
        'schemaVersion': 'paw.binary-installer.v1', 'productVersion': version,
        'productSourceCommit': commit, 'installerSourceCommit': installer_commit,
        'platform': 'darwin', 'architecture': 'arm64', 'minimumMacOS': '14.0',
        'signed': False, 'notarized': False, 'channel': 'unsigned-preview',
        'optionalComponentsNotBundled': ['patched Squirrel', 'MLX model weights'],
        'files': inventory(payload),
    }
    (payload / 'installer-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    verify(payload)
    (contents / 'MacOS').mkdir()
    run('xcrun', 'swiftc', '-O', '-target', 'arm64-apple-macosx14.0', '-framework', 'AppKit',
        ROOT / 'macos/PAWInstaller/main.swift', '-o', contents / 'MacOS/PAWInstaller')
    shutil.copy2(ROOT / 'scripts/support/run_binary_installer.sh', resources / 'install.sh')
    shutil.copy2(control / 'Contents/Resources/RagImeIcon.icns', resources / 'PAW.icns')
    (contents / 'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier': 'com.paw.installer', 'CFBundleExecutable': 'PAWInstaller',
        'CFBundleName': 'Install Personal Agent Workbench', 'CFBundleDisplayName': 'Install Personal Agent Workbench',
        'CFBundleShortVersionString': version, 'CFBundleVersion': '1',
        'CFBundlePackageType': 'APPL', 'CFBundleIconFile': 'PAW',
        'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True,
    }))
    (volume / '安装说明.txt').write_text(
        'Personal Agent Workbench — Apple Silicon 未签名预览版\n\n'
        '1. 双击 Install Personal Agent Workbench，点击“安装”。无需安装开发工具。\n'
        '2. 安装完成后打开 PAW。听写服务、麦克风和辅助功能在 Input Studio 中设置。\n'
        '3. 应用安装在 ~/Applications，数据和运行环境在 ~/Library/Application Support/RagIme。\n\n'
        '此预览版未获得 Developer ID 签名或 Apple 公证。macOS 可能阻止首次打开；\n'
        '确认下载来源与 SHA256 后，可在系统设置 > 隐私与安全性中选择“仍要打开”。\n'
        '未包含可选的 Squirrel 输入法和 MLX 模型权重。首次安装需要配置自己的模型/听写服务。\n'
        '已有数据保留，旧组件备份在 RagIme/InstallerRecovery，安装日志在 ~/Library/Logs/PAW Installer。\n'
        '这不是拖拽安装的独立 app：请运行安装器，让配套服务一并安装。\n'
        '源码与第三方许可证位于安装器 Contents/Resources/payload/corresponding-source 和各运行环境目录。\n',
        encoding='utf-8')
    # Sign only the wrapper. --deep would rewrite nested Node/Python/app content
    # after inventory and invalidate the payload manifest.
    run('/usr/bin/codesign', '--force', '--sign', '-', app)
    verify(payload)
    dmg = output / f'Personal-Agent-Workbench-{version}-macOS-arm64-unsigned.dmg'
    run('/usr/bin/hdiutil', 'create', '-volname', 'PAW Installer', '-srcfolder', volume,
        '-format', 'UDZO', '-ov', dmg)
    run('/usr/bin/hdiutil', 'verify', dmg)
    import hashlib
    with dmg.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    (output / 'SHA256SUMS.txt').write_text(f'{checksum}  {dmg.name}\n')
    (output / 'build-receipt.json').write_text(json.dumps({
        key: value for key, value in manifest.items() if key != 'files'
    } | {'dmg': str(dmg), 'sha256': checksum, 'bytes': dmg.stat().st_size}, indent=2) + '\n')
    print(dmg)


if __name__ == '__main__':
    main()
