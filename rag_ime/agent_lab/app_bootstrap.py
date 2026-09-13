"""Explicit, portable environment setup. Copied as launch.py into frozen Apps.

This module does not own App requests, retrieval, or model execution. It prepares
an interpreter and a pinned public encoder, then execs the existing app.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Matches uv.lock and the verified KnowledgeRuntime environment. Direct encoder
# dependencies are pinned; pip resolves platform-specific transitive wheels.
DENSE_DEPENDENCIES = {
    'torch': '2.13.0',
    'sentence-transformers': '5.6.0',
    'transformers': '5.13.1',
    'huggingface-hub': '1.23.0',
}


class SetupError(ValueError):
    pass


def embedding_config(root: Path) -> dict | None:
    try:
        spec = json.loads((root / 'app.json').read_text(encoding='utf-8'))
        knowledge = spec.get('knowledge') or {}
        if (knowledge.get('profile') or {}).get('mode') not in {'dense', 'hybrid'}:
            return None
        embedding = (knowledge.get('dense') or {}).get('provider') or knowledge.get('embedding') or {}
        model, revision = embedding.get('model', ''), embedding.get('modelRevision', '')
        if (embedding.get('provider') != 'sentence-transformers'
                or not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', model)
                or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision)):
            raise SetupError('app.json 缺少公开 Embedding 仓库与固定 40 位 commit，请重新导出。')
        return embedding
    except (OSError, ValueError, AttributeError) as exc:
        if isinstance(exc, SetupError):
            raise
        raise SetupError('无法读取 app.json，请完整解压应用 ZIP 后运行。') from exc


def missing_dependencies(embedding: dict | None) -> list[str]:
    if embedding is None:
        return []
    missing = []
    for package, expected in DENSE_DEPENDENCIES.items():
        try:
            actual = version(package)
        except PackageNotFoundError:
            actual = ''
        if actual.split('+')[0] != expected:
            missing.append(f'{package}=={expected}')
    return missing


def runtime_environment(root: Path) -> dict:
    environment = dict(os.environ)
    # The runtime's frozen encoder uses the standard HF cache. A caller can
    # explicitly reuse an existing cache; default installs stay inside this App.
    environment.setdefault('HF_HUB_CACHE', str(root / '.app-model-cache' / 'hub'))
    environment['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    return environment


def prepare_encoder(embedding: dict, *, download: bool) -> None:
    try:
        from sentence_transformers import SentenceTransformer
        SentenceTransformer(embedding['model'], revision=embedding['modelRevision'],
                            cache_folder=os.environ['HF_HUB_CACHE'],
                            local_files_only=not download, trust_remote_code=False,
                            token=False, device='cpu')
    except Exception as exc:
        # Provider errors may include URLs or credentials from user environment;
        # expose the failed component and recovery, not raw exception strings.
        raise SetupError(f"固定 Embedding 模型 {embedding['model']}@{embedding['modelRevision']} "
                         f"尚不可用（{type(exc).__name__}）。请运行 python3 launch.py --setup；"
                         '首次安装需要网络及足够磁盘空间。') from exc


def probe(root: Path, *, download: bool = False, dependencies_only: bool = False) -> int:
    embedding = embedding_config(root)
    missing = missing_dependencies(embedding)
    if missing:
        raise SetupError('缺少或版本不符：' + ', '.join(missing) + '。请运行 python3 launch.py --setup。')
    if dependencies_only:
        return 0
    import sqlite3
    try:
        connection = sqlite3.connect(':memory:')
        try:
            connection.execute('CREATE VIRTUAL TABLE check_fts USING fts5(text)')
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SetupError('当前 Python 缺少 SQLite FTS5，请使用带 FTS5 的 Python 3.10+ 重新安装。') from exc
    if embedding:
        prepare_encoder(embedding, download=download)
    print('运行环境与固定模型检查通过。' if embedding else '运行环境检查通过。', flush=True)
    return 0


def _run_probe(python: Path, root: Path, environment: dict, *options: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(python), str(root / 'launch.py'), '--_probe', *options],
                          env=environment, capture_output=True, text=True, check=False)


def setup(root: Path, environment: dict) -> Path:
    import venv
    python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        print('正在创建此应用的 Python 环境…', flush=True)
        try:
            venv.EnvBuilder(with_pip=True).create(root / '.venv')
        except Exception as exc:
            raise SetupError('无法创建 .venv。请安装包含 venv/ensurepip 的 Python 3.10+，然后重新运行 --setup。') from exc
    dependencies = _run_probe(python, root, environment, '--_dependencies-only')
    if dependencies.returncode:
        print('正在安装此版本的语义检索依赖…', flush=True)
        installed = subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                                    '-r', str(root / 'requirements-app.txt')],
                                   env=environment, capture_output=True, text=True, check=False)
        if installed.returncode:
            raise SetupError('依赖安装未完成。需要 requirements-app.txt 中的固定版本及适配此 Python/系统的 wheel；'
                             '请检查网络、磁盘和 Python 版本后重新运行 --setup。现有缓存会保留。')
    # An already usable environment does not reinstall or redownload anything.
    checked = _run_probe(python, root, environment)
    if checked.returncode:
        print('正在准备 app.json 记录的固定 Embedding 模型…', flush=True)
        checked = _run_probe(python, root, environment, '--_download')
    if checked.returncode:
        raise SetupError(checked.stdout.strip() or '环境检查未通过，请重新运行 --setup。')
    print('安装完成。运行 python3 launch.py，或在 macOS 双击 Start.command。', flush=True)
    return python


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description='安装或启动这个独立 App；安装只在显式 --setup 时执行。')
    parser.add_argument('--setup', action='store_true', help='创建本 App 的 .venv、安装依赖和固定模型，然后退出')
    parser.add_argument('--check', action='store_true', help='离线检查环境与固定模型，不安装或启动服务')
    parser.add_argument('--python', type=Path, help='显式复用已有 Python 环境，仅检查/启动，不修改该环境')
    parser.add_argument('--paw', action='store_true', help='使用本机 PAW 的 Pi 与知识库，无需为此 App 重装模型环境')
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', 'localhost'])
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--_probe', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--_download', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--_dependencies-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = (root or Path(__file__).resolve().parent).resolve()
    try:
        embedding_config(root)
        environment = runtime_environment(root)
        if args.paw:
            environment.setdefault('APP_PAW_GATEWAY_URL', 'http://127.0.0.1:8768')
        if args._probe:
            os.environ.update(environment)
            return probe(root, download=args._download, dependencies_only=args._dependencies_only)
        if args.setup:
            if args.python:
                raise SetupError('--setup 只安装此 App 的 .venv；--python 指定的既有环境不会被修改。')
            setup(root, environment)
            return 0
        local_python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        python = args.python or (Path(sys.executable) if environment.get('APP_PAW_GATEWAY_URL') and not local_python.is_file() else local_python)
        python = python.absolute()
        if not python.is_file():
            raise SetupError('尚未安装此应用环境。请先运行 python3 launch.py --setup，再运行 python3 launch.py。')
        # PAW owns retrieval in gateway mode. Pure standalone always checks its
        # own encoder; no implicit network/download occurs during normal launch.
        if not environment.get('APP_PAW_GATEWAY_URL') or args.check:
            checked = _run_probe(python, root, environment)
            if checked.returncode:
                raise SetupError(checked.stdout.strip() or 'Python 检查失败，请运行 python3 launch.py --setup。')
            if args.check:
                print(checked.stdout.strip(), flush=True)
                return 0
        os.execve(str(python), [str(python), str(root / 'app.py'), '--host', args.host, '--port', str(args.port)], environment)
        return 0
    except (SetupError, OSError) as exc:
        print(str(exc) if isinstance(exc, SetupError) else '无法运行 Python。请核对此应用目录权限并重新运行 --setup。', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
