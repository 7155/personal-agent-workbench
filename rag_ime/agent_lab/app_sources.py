"""Freeze explicitly selected application sources; never run them at intake."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .project_artifacts import _text
from .projects import AgentLabProjectValidationError


SOURCE_SCHEMA = 'paw.lab-app-source.v1'
EXPORT_RECIPE_VERSION = 'paw.lab-app-export.v13'
_ACTION_ID = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_BLOCKED_FILES = {'auth.json', 'credentials.json', 'pi-providers.json', 'models.json', 'package-lock.json'}
_EXPORT_FILES = {'app.json', 'app.py', 'app_research.py', 'readme.md', 'paw-app.json', 'paw-runtime.json', 'agent-ui.js', 'agent-ui.css', 'launch.py', 'start.command', 'requirements-app.txt'}


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 500 or '\\' in value:
        raise AgentLabProjectValidationError('应用文件需要相对路径。')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'.', '..'} or part.startswith('.') for part in value.split('/')):
        raise AgentLabProjectValidationError('应用文件不能越过源码目录或包含隐藏文件。')
    if path.name.lower() in _BLOCKED_FILES or path.suffix.lower() in {'.env', '.pem', '.key', '.sqlite', '.db'}:
        raise AgentLabProjectValidationError('此文件属于凭据、本机配置或数据库，不能打包到应用。')
    return path.as_posix()


def _read(root: Path, value: Any) -> tuple[str, str]:
    path = _relative(value)
    candidate = root
    for part in PurePosixPath(path).parts:
        candidate /= part
        if candidate.is_symlink(): raise AgentLabProjectValidationError('应用源文件不能是符号链接。')
    if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
        raise AgentLabProjectValidationError(f'应用文件不存在：{path}')
    if candidate.stat().st_size > 500_000: raise AgentLabProjectValidationError(f'应用文件超过 500 KB：{path}')
    try: content = candidate.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc: raise AgentLabProjectValidationError(f'应用文件不能作为 UTF-8 文本读取：{path}') from exc
    if '\0' in content: raise AgentLabProjectValidationError('应用文件包含二进制内容。')
    return path, content


def validate_input_schema(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) - {'type','properties','required','additionalProperties'} or value.get('type') != 'object':
        raise AgentLabProjectValidationError('应用输入 schema 需要一个对象。')
    properties = value.get('properties')
    if not isinstance(properties, dict) or len(properties) > 40:
        raise AgentLabProjectValidationError('应用输入最多包含 40 个字段。')
    for key, field in properties.items():
        if not _ACTION_ID.fullmatch(key) or not isinstance(field, dict) or set(field) - {'type','title','description','enum','maxLength','minimum','maximum'}:
            raise AgentLabProjectValidationError('应用输入字段格式无效。')
        kind = field.get('type')
        if kind not in {'string','number','integer','boolean'}: raise AgentLabProjectValidationError('应用字段支持文本、数字、整数和布尔值。')
        for name in {'title','description'} & field.keys(): _text(field[name], '输入说明', 2000)
        if 'maxLength' in field and (kind != 'string' or type(field['maxLength']) is not int or not 1 <= field['maxLength'] <= 24_000):
            raise AgentLabProjectValidationError('文本长度上限无效。')
        for name in {'minimum','maximum'} & field.keys():
            if kind not in {'number','integer'} or type(field[name]) not in {int,float} or not float('-inf') < field[name] < float('inf'):
                raise AgentLabProjectValidationError('数字范围无效。')
        if 'enum' in field:
            choices = field['enum']
            if kind != 'string' or not isinstance(choices, list) or not 1 <= len(choices) <= 100 or any(not isinstance(item,str) for item in choices):
                raise AgentLabProjectValidationError('可选值需要文本列表。')
    required = value.get('required', [])
    if not isinstance(required, list) or any(not isinstance(item,str) for item in required) or set(required) - properties.keys():
        raise AgentLabProjectValidationError('必填字段需要属于当前输入。')
    return {'type':'object','properties':copy.deepcopy(properties),'required':required,'additionalProperties':False}


def freeze_source(workspace: Path, directory: str, default_model: dict[str,str], *, freeze_knowledge: Callable | None = None,
                  select_evaluation: Callable | None = None, evaluation_selection: dict | None = None) -> dict:
    workspace = workspace.resolve(strict=True)
    directory = _relative(directory)
    root = workspace
    for part in PurePosixPath(directory).parts:
        root /= part
        if root.is_symlink(): raise AgentLabProjectValidationError('应用目录不能是符号链接。')
    if not root.is_dir() or not root.resolve().is_relative_to(workspace): raise AgentLabProjectValidationError('应用源码需要位于此项目的执行目录内。')
    root = root.resolve()
    _, encoded = _read(root, 'app.json')
    try: value = json.loads(encoded)
    except ValueError as exc: raise AgentLabProjectValidationError('app.json 不是有效 JSON。') from exc
    fields = {'schemaVersion','title','description','html','skill','context','actions','model','knowledge','externalWorkspace','appearance','evaluationSelection','workflow'}
    if not isinstance(value, dict) or set(value) - fields or value.get('schemaVersion') != SOURCE_SCHEMA:
        raise AgentLabProjectValidationError('app.json 需要 paw.lab-app-source.v1 格式。')
    if evaluation_selection is not None:
        # A UI selection overrides the source declaration for this immutable
        # preparation only; it never edits the user's working app.json.
        value['evaluationSelection'] = copy.deepcopy(evaluation_selection)
    selection = None
    if 'evaluationSelection' in value:
        if select_evaluation is None:
            raise AgentLabProjectValidationError('当前应用准备器没有连接评测记录，请保留所选版本后重新连接。')
        selection = select_evaluation(value['evaluationSelection'])
        configuration = selection['configuration']
        if not configuration.get('applicationMethod'):
            raise AgentLabProjectValidationError('这次评测没有冻结应用方法，请先验证需要导出的 Skill。')
        # The completed execution owns this selection. Never silently export
        # the source folder's later model, prompt, method, or retrieval values.
        value['model'] = {key: configuration[key] for key in ('provider', 'model', 'thinkingLevel')}
        if selection.get('knowledge'):
            binding = selection['knowledge']
            declared = value.get('knowledge') or {}
            value['knowledge'] = {'indexId': binding['indexId'], 'profile': binding['profile'],
                                  'queryField': declared.get('queryField', 'question')}
        elif value.get('knowledge'):
            raise AgentLabProjectValidationError('所选评测未使用此知识库，不能将它标记为已验证的知识库应用。')
    title = _text(value.get('title'), '应用名称', 100)
    description = _text(value.get('description', ''), '应用说明', 3000, empty=True)
    context = value.get('context', [])
    if not isinstance(context, list) or len(context) > 32: raise AgentLabProjectValidationError('应用材料最多 32 份。')
    files = dict(_read(root, path) for path in [value.get('html'), value.get('skill'), *context])
    if any(path.lower() in _EXPORT_FILES or path == 'knowledge_runtime.py' or path.split('/')[0] in {'knowledge','knowledge_owner'} for path in files):
        raise AgentLabProjectValidationError('app.json、app.py、README.md 和 paw-app.json 是导出运行器的保留文件名，请将业务材料换一个文件名。')
    if not str(value['html']).endswith('.html') or not str(value['skill']).endswith('SKILL.md'):
        raise AgentLabProjectValidationError('应用需要 HTML 入口和 SKILL.md 方法文件。')
    if selection:
        files[_relative(value['skill'])] = selection['configuration']['applicationMethod']['body']
    if sum(len(text.encode()) for text in files.values()) > 2_000_000:
        raise AgentLabProjectValidationError('应用源文件合计超过 2 MB。')
    actions = value.get('actions')
    if not isinstance(actions, list) or not 1 <= len(actions) <= 16:
        raise AgentLabProjectValidationError('应用需要 1–16 项声明的操作。')
    normalized = []
    for action in actions:
        if not isinstance(action, dict) or set(action) - {'id','title','prompt','inputSchema','kind'} or not {'id','title','prompt','inputSchema'}.issubset(action) or not isinstance(action['id'],str) or not _ACTION_ID.fullmatch(action['id']):
            raise AgentLabProjectValidationError('应用操作需要 id、title、prompt 和 inputSchema。')
        kind = action.get('kind','completion')
        if kind not in {'completion','retrieval'} or (kind == 'retrieval' and 'knowledge' not in value):
            raise AgentLabProjectValidationError('检索操作需要声明应用知识库。')
        normalized.append({'id':action['id'],'title':_text(action['title'],'操作名称',120),
                           'prompt':_text(selection['configuration']['prompt'] if selection and kind == 'completion' else action['prompt'],
                                          '操作方法',30_000,empty=bool(selection and kind == 'completion')),
                           'inputSchema':validate_input_schema(action['inputSchema']),
                           **({'kind':kind} if kind != 'completion' else {})})
    if len({item['id'] for item in normalized}) != len(normalized): raise AgentLabProjectValidationError('应用操作标识不能重复。')
    model = value.get('model', default_model)
    if not isinstance(model, dict) or set(model) - {'provider','model','thinkingLevel','prompt'}:
        raise AgentLabProjectValidationError('应用模型配置无效。')
    model = {key:_text(model.get(key, default_model.get(key)), '模型配置', 240) for key in ['provider','model','thinkingLevel']}
    if model['thinkingLevel'] not in {'off','minimal','low','medium','high','xhigh','max'}:
        raise AgentLabProjectValidationError('应用推理强度无效。')
    spec = {'schemaVersion':SOURCE_SCHEMA,'title':title,'description':description,'html':_relative(value['html']),
            'skill':_relative(value['skill']),'context':[_relative(path) for path in context],'actions':normalized,'model':model}
    if selection:
        method = selection['configuration']['applicationMethod']
        spec['evaluationSelection'] = {key: copy.deepcopy(selection[key]) for key in
            ('suiteId', 'jobId', 'variant', 'snapshotId', 'configurationSha256', 'summary') if key in selection}
        spec['evaluationSelection'].update(scope='application_method_and_configuration',
            applicationMethod={key: copy.deepcopy(method[key]) for key in ('title', 'sha256', 'source') if key in method})
    if 'appearance' in value:
        appearance = value['appearance']
        if not isinstance(appearance, dict) or set(appearance) - {'accent','icon','colorScheme'} or not {'accent','icon'}.issubset(appearance):
            raise AgentLabProjectValidationError('应用外观需要主题色和图标。')
        icon = appearance['icon']
        if not isinstance(appearance['accent'],str) or appearance['accent'] not in {'cyan','blue','violet','amber','green','rose','slate'} or not isinstance(icon, dict) or set(icon) != {'symbol','background'}:
            raise AgentLabProjectValidationError('应用外观配置无效。')
        if not isinstance(icon['symbol'],str) or icon['symbol'] not in {'analytics','assistant','document','commerce'} or not isinstance(icon['background'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', icon['background']):
            raise AgentLabProjectValidationError('应用图标需要已支持的符号及十六进制背景色。')
        if appearance.get('colorScheme', 'inherit') not in ('inherit', 'light', 'dark'):
            raise AgentLabProjectValidationError('应用配色模式无效。')
        spec['appearance'] = copy.deepcopy(appearance)
    if 'externalWorkspace' in value:
        from .app_runtime import validate_external_workspace
        try: spec['externalWorkspace'] = validate_external_workspace(value['externalWorkspace'])
        except ValueError as exc: raise AgentLabProjectValidationError(str(exc)) from exc
    resource_files = {}
    if 'knowledge' in value:
        if freeze_knowledge is None:
            raise AgentLabProjectValidationError('当前应用准备器没有连接 Knowledge，不能省略知识库依赖。')
        resources = freeze_knowledge(value['knowledge'])
        spec['knowledge'] = resources['knowledge']
        resource_files = resources['files']
        field = spec['knowledge']['queryField']
        if any(action['inputSchema']['properties'].get(field,{}).get('type') != 'string'
               or field not in action['inputSchema']['required'] for action in normalized):
            raise AgentLabProjectValidationError('每个知识库操作都需要声明必填的问题文本字段。')
    if 'workflow' in value:
        from .app_research import normalize_workflow
        if not spec.get('knowledge'):
            raise AgentLabProjectValidationError('按需研究需要冻结的应用知识库。')
        try:
            spec['workflow'] = normalize_workflow(value['workflow'], spec['knowledge']['profile'])
        except ValueError as exc:
            raise AgentLabProjectValidationError(str(exc)) from exc
        if 'historyField' in spec['workflow'] and any(action['inputSchema']['properties'].get(spec['workflow']['historyField'], {}).get('type') != 'string' for action in normalized):
            raise AgentLabProjectValidationError('研究历史字段需要在每个操作声明为文本。')
        resource_files = {**resource_files, 'app_research.py': Path(__file__).with_name('app_research.py').read_text(encoding='utf-8')}
        if selection:
            spec['evaluationSelection']['workflowValidated'] = False
    from .app_bootstrap import DENSE_DEPENDENCIES
    dense = spec.get('knowledge', {}).get('profile', {}).get('mode') in {'dense', 'hybrid'}
    bootstrap_files = {
        'launch.py': Path(__file__).with_name('app_bootstrap.py').read_text(encoding='utf-8'),
        'requirements-app.txt': ('\n'.join(f'{name}=={version}' for name, version in DENSE_DEPENDENCIES.items()) + '\n'
                                 if dense else '# This App only requires Python standard-library modules.\n'),
        'Start.command': '#!/bin/sh\ncd "$(dirname "$0")" || exit 1\npython3 launch.py '
                         + ('--paw ' if spec.get('workflow') else '')
                         + '"$@"\nstatus=$?\nif [ "$status" -ne 0 ]; then printf "\\nPress Enter to close…"; read -r reply; fi\nexit "$status"\n',
    }
    runtime = Path(__file__).with_name('app_runtime.py').read_text(encoding='utf-8')
    ui_files = {}
    if 'pawAgentUI.mount' in files[spec['html']]:
        built = Path(__file__).resolve().parents[2]/'control-center-web/.generated/portable-agent-ui'
        try: ui_files = {name:(built/name).read_text() for name in ('agent-ui.js','agent-ui.css')}
        except OSError as exc: raise AgentLabProjectValidationError('请先运行前端 build:app-ui，准备共享 Agent 控件。') from exc
    digest = hashlib.sha256(json.dumps({'spec':spec,'files':files,'uiFiles':ui_files,'resourceFiles':resource_files,'bootstrapFiles':bootstrap_files,'runtimeSource':runtime,'exportRecipeVersion':EXPORT_RECIPE_VERSION},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'spec':spec,'files':files,'runtimeSource':runtime,'runtimeSha256':hashlib.sha256(runtime.encode()).hexdigest(),
            'bootstrapFiles':bootstrap_files,'contentHash':digest,'sourceDirectory':directory,'exportRecipeVersion':EXPORT_RECIPE_VERSION,
            **({'resourceFiles':resource_files} if resource_files else {}), **({'uiFiles':ui_files} if ui_files else {})}


def export_zip(version: dict, target: str) -> tuple[str, bytes]:
    if target not in {'standalone','paw'}: raise AgentLabProjectValidationError('不支持此导出目标。')
    spec, files = version['spec'], {**version['files'], **version.get('resourceFiles',{}), **version.get('uiFiles',{}), **version.get('bootstrapFiles',{})}
    files['app.json'] = json.dumps(spec, ensure_ascii=False, indent=2)+'\n'
    files['app.py'] = version['runtimeSource']
    files['paw-runtime.json'] = json.dumps({'appId':version['appId'], 'version':version['version'],
        'contentHash':version['contentHash'], 'sourceHash':hashlib.sha256(json.dumps(
            {'spec':spec,'files':version['files']},sort_keys=True,ensure_ascii=False).encode()).hexdigest()}, indent=2)+'\n'
    files['README.md'] = f"""# {spec['title']}

{spec['description']}

{'本研究版使用本机 PAW 的 Pi 及冻结知识库。保持 PAW 运行后，双击 Start.command，或运行 python3 launch.py --paw；无需为此包重装向量模型。基础入口需要 Python 3.10+。' if spec.get('workflow') else '这是独立可运行的应用源码包。基础运行器使用 Python 3.10+ 标准库，不需要 PAW 后端；语义检索等附加功能的依赖见下方知识库运行说明。'}

独立本地检索环境的首次安装，在解压目录运行 `python3 launch.py --setup`。它只在显式安装时创建此 App 的 `.venv`，
安装 `requirements-app.txt` 中的依赖，并准备固定版本的模型；中断后重新执行同一命令即可继续。
之后运行 `python3 launch.py`，或在 macOS 双击 `Start.command`，打开终端输出的本机地址。
解压工具若未保留执行位，可先运行 `chmod +x Start.command`。
默认监听 `127.0.0.1:8080`；换端口用 `python3 launch.py --port 8081`。
`python3 launch.py --check` 只做离线环境检查，正常启动不会安装依赖或下载模型。
高级用法：`python3 launch.py --python /path/to/existing/python` 显式复用既有环境，不修改它。
安装失败会保留缓存并指出恢复入口。Python 及第三方 wheel 需要适配当前系统。
调用模型前，在当前终端设置 `APP_API_BASE_URL`（HTTPS、以 API v1 路径结尾）和
`APP_API_KEY`。不要把凭据写入源码。`APP_MODEL`、`APP_REASONING_EFFORT` 可选；
默认模型为 `{spec['model']['model']}`，推理强度 `{spec['model']['thinkingLevel']}`。
模型服务需要支持 OpenAI Chat Completions 协议。修改模型或方法后需要重新验证效果。

也可设置 `APP_PAW_GATEWAY_URL=http://127.0.0.1:8768` 连接持有此版本的本机 PAW。
此模式通过现有 App 调用交给 Pi，复用 PAW 的模型与 OAuth 登录，不需要复制任何密钥。
PAW 负责每轮的资料检索、方法组装和模型执行；PAW 服务必须保持运行。
模型选择只影响下一次调用，调用回执记录选择，不修改此冻结版本或其评测结果。

HTML 使用 `window.pawApp.invoke(actionId, input)`，返回包含 `text` 与 `usage` 的结果。
页面和输入结构属于此应用。SKILL.md 与材料均为此冻结版本的副本。
本版本执行声明的知识检索或文本生成操作；没有订单、支付、发货或其他外部业务写入接口。

运行地址默认仅为本机。调用输入与回执保存在本目录的 `.app-state.sqlite3`，文件权限为仅当前用户可读写。
请求按 requestId 持久去重；刷新或重启不会自动重放。未收到完成回执的旧调用保持“结果未确认”，
请先核对模型服务记录。浏览器在连接中断后再次提交同样的输入时，会核对原请求。
`window.pawApp.history()` 可读取最近 20 次调用，应用界面可据此恢复输入或结果。
默认最多同时运行 4 次调用，最多保留 5000 条记录；归档记录后可以开启新记录。
请保管好本机记录文件，不要把它与凭据一同发布。若用于正式服务，需要自行配置认证和部署环境。

源版本指纹：`{version['contentHash']}`。打包验证仅证明文件与运行合同，不代表业务质量。
"""
    if spec.get('knowledge'):
        knowledge = spec['knowledge']
        mode = knowledge['profile']['mode']
        retrieval_name = {'lexical': 'FTS5 关键词检索', 'dense': '语义向量检索', 'hybrid': '关键词与语义混合检索'}.get(mode, mode)
        files['README.md'] += f"""\n## 知识库运行依赖

此包冻结了 {knowledge['documentCount']:,} 篇独立正文的 {knowledge['chunkCount']:,} 个切片，
保留 {knowledge['sourceCount']:,} 个来源。实际运行使用冻结的 {retrieval_name}，
Top K 为 {knowledge['profile']['topK']}，回答证据最多 {knowledge['profile']['contextChars']:,} 个字符。
首次检索会在 `.knowledge-cache` 中恢复本包自己的索引，不需要 PAW 服务或全库上下文拼接。
需要 Python 的 SQLite 支持 FTS5；原评测答案、标注、运行日志与模型密钥没有随包导出。
声明为 retrieval 的操作只检索来源，不调用回答模型；completion 操作还需要上述模型配置。
更改知识库、检索配置、模型或方法后应重新验证。检索成功不代表回答质量已经通过。
"""
        if mode in {'dense', 'hybrid'}:
            embedding = knowledge.get('embedding') or knowledge.get('dense', {}).get('provider', {})
            files['README.md'] += f"""\n语义检索还需要 `sentence-transformers` 及本版固定的 Embedding 模型
`{embedding.get('model', '见 app.json 的 embedding 配置')}`，commit `{embedding.get('modelRevision', '')}`。
`--setup` 安装本版固定的 torch / sentence-transformers / transformers / huggingface-hub，
并从公开仓库准备这个 commit 的模型（不使用本机 Hugging Face 登录 token，不执行远程模型代码）。
依赖版本对应本版 uv.lock；平台相关的间接依赖由 pip 解析，未承诺跨平台逐字节相同环境。
模型默认缓存在此 App 的 `.app-model-cache/hub`；可以显式设置 `HF_HUB_CACHE` 复用已有缓存。
首次安装需要联网和模型所需磁盘空间，后续启动只读取本地固定模型。模型权重与 API 密钥不在此 ZIP 中。
随包向量会恢复到本包自己的缓存，不需要重算整库。依赖不可用时会报告原因，不会自动改为关键词检索。
"""
    if spec.get('evaluationSelection'):
        selection = spec['evaluationSelection']
        files['README.md'] += f"""\n## 所选实验

此版本使用实验 `{selection['jobId']}` 的 `{selection['variant']}` 配置，
评测快照为 `{selection['snapshotId']}`。SKILL.md、模型、问题方法和检索配置来自该次已完成回执，
完整身份保存在 app.json 的 evaluationSelection 中。后续编辑源码不会改写已导出的版本。
这记录方法与配置的验证来源；App 界面和部署仍需另行试用验收，不代表所有新问题都已通过。
"""
    if spec.get('workflow'):
        # Research never silently degrades to the direct completion adapter.
        files['README.md'] = files['README.md'].replace('基础运行器使用 Python 3.10+ 标准库，不需要 PAW 后端；',
            '基础页面运行器使用 Python 3.10+ 标准库；此研究版需要 PAW 的 Pi 执行服务；')
        files['README.md'] += f"""\n## 按需研究执行依赖

此版本必须设置 `APP_PAW_GATEWAY_URL=http://127.0.0.1:8768`，连接持有本冻结版本的 PAW。
PAW 已运行时可直接用 `APP_PAW_GATEWAY_URL=http://127.0.0.1:8768 python3 app.py` 启动，
此桥接入口只需 Python 标准库，无需先安装本地语义模型或复制 API 密钥。
Pi 使用 `lab_research` 在冻结快照内发现论文、文内定位、按页或切片读取、按缺口搜索；
最多 {spec['workflow']['maxToolCalls']} 次知识工具操作，其中最多 {spec['workflow']['maxSearchCalls']} 次搜索，
总证据预算 {spec['knowledge']['profile']['contextChars']} 字符，为阅读预留 {spec['workflow']['readReserveChars']} 字符。
来源与预算回执跟随原 App 调用保存。没有 PAW/Pi 连接时，本版不会退化为一次检索加直接模型回答。
`APP_API_BASE_URL` 与 `APP_API_KEY` 不能代替研究工具执行服务；本地独立模型循环尚未随包提供。
随包 reader 可读取完整冻结文本与真实页码，原 PDF 文件和机器路径不属于读取契约。
若版本还选择了 Golden 方法实验，该旧单包实验没有验证此新增工具工作流；需要另行验证研究质量。
"""
    if target == 'paw':
        files['paw-app.json'] = json.dumps({'schemaVersion':'paw.lab-app-package.v1','source':'app.json',
                                           'contentHash':version['contentHash'],'runtime':'pi-completion'},indent=2)+'\n'
        files['README.md'] += ('\nPAW 应用包：本机已准备的版本可直接在 Lab 交付页添加至 PAW。跨 PAW 实例的知识库包导入尚未接入；迁移时需在目标项目导入资料并重新绑定索引、准备应用。独立包可直接使用随包快照进行本地检索。\n'
                               if spec.get('knowledge') else '\nPAW 应用包：本机可直接在 Lab 交付页添加至 PAW。迁移时将源码解压到另一 Lab 项目的执行目录，再让项目 Agent 准备这个应用。\n')
    if spec.get('externalWorkspace'):
        workspace = spec['externalWorkspace']
        files['README.md'] += f"""\n## 外部工作台

此应用还连接 `{workspace['title']}`：{workspace['url']}。
该工作台及其后端需要单独启动；其源码、数据、登录态和运行结果没有随此包复制。
PAW 与独立 App 均提供单独的工作台页面和浏览器入口。
独立部署可用 `APP_WORKSPACE_URL` 指定目标机器上已启动的工作台地址（HTTPS 或本机 HTTP）。
此设置只改变浏览器目的地，不改变冻结资料、方法或模型，也不授予工作台模型调用权限。
资料问答不会自动调用该工作台，也不能以回答文本证明其任务已经执行。
外部工作台不能通过嵌入页面获得此应用的模型调用或历史读取权限。
"""
    output = io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path, text in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980,1,1,0,0,0)); info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if path == 'Start.command' else 0o100644) << 16
            archive.writestr(info, text.encode())
    return f"lab-app-v{version['version']}-{target}.zip", output.getvalue()
