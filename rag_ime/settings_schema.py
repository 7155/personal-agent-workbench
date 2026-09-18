from __future__ import annotations

import copy
import json
from typing import Any, Mapping


DEFAULT_SETTINGS: dict[str, object] = {
    "identity": {
        "productName": "PAW",
        "assistantName": "Agent",
        "tagline": "记得你，也陪你做事",
    },
    "interaction": {
        "composition": {
            "showPrediction": False,
            "showOnlyRime": True,
            "numberKeys": "select_rime_candidate",
        },
        "postCommit": {
            "enabled": True,
            "idleTriggerMs": 220,
            "minDeltaChars": 2,
            "maxCallsPer10s": 6,
            "cooldownMs": 1500,
            "showPendingStatus": False,
            "pendingStatusDelayMs": 600,
            "numberKeys": "pass_through",
            "tabAction": "accept_top_prediction",
            "optionNumber": "select_prediction_by_ordinal",
            "escape": "dismiss_prediction",
            "panelTtlMs": 4000,
            "modelBudgetMs": 900,
        },
    },
    "display": {
        "badges": {
            "rime": "词",
            "model": "模",
            "rag": "查",
            "memory": "忆",
            "status": "查忆",
            "action": "生成",
            "raw_english": "input",
        },
        "colors": {
            "rime": "default",
            "model": "blue",
            "rag": "teal",
            "memory": "purple",
            "status": "gray",
            "action": "blue",
            "raw_english": "gray",
        },
        "maxCompositionCandidates": 8,
        "maxPostCommitCandidates": 5,
        "showSourceBadge": True,
        "showDiagnosticsInline": False,
        "statusRowStyle": "compact",
        "candidateFontSize": 14,
        "panelStyle": "compact",
        "fadeAnimation": True,
        "maxWidth": 520,
    },
    "rag": {
        "hybrid": {"enabled": True, "budgetMs": 400},
        "lanes": {
            "bm25Raw": True,
            "bm25Tags": True,
            "vectorRaw": True,
            "vectorTagBoost": True,
            "tagMemo": True,
            "timeDailyBook": True,
            "feedback": True,
        },
        "weights": {
            "bm25Raw": 1.0,
            "bm25Tags": 1.15,
            "vectorRaw": 1.05,
            "vectorTagBoost": 1.05,
            "tagMemo": 1.1,
            "timeDailyBook": 0.9,
            "feedback": 1.2,
        },
        "antiEcho": {
            "blockRawHistoryLongCandidate": True,
            "blockCommittedTailEcho": True,
            "blockSelectedTextEcho": True,
        },
    },
    "lexiconOrganization": {
        "enabled": True,
        "runsPerDay": 2,
    },
    "memory": {
        "enabled": True,
        "rawHistoryRedacted": True,
        "directCandidateDefault": False,
        "retentionDays": 30,
        "shortTermItems": 20,
        "shortTermTtlMinutes": 30,
        "allowProjectFallback": True,
        "allowAppFallback": True,
        "allowGlobalMemory": True,
        "archiveInactiveDays": 60,
        "timeDecay": {
            "temporaryHalfLifeDays": 14,
            "projectHalfLifeDays": 120,
            "topicBookHalfLifeDays": 180,
            "stablePreferenceHalfLifeDays": 365,
        },
        "automaticOrganization": {
            "enabled": True,
            "model": "openai-codex/gpt-5.6-luna",
            "thinkingLevel": "max",
            "runsPerDay": 2,
            "includeAgentDialogue": True,
        },
        "dreaming": {
            "enabled": True,
            "model": "openai-codex/gpt-5.6-luna",
            "thinkingLevel": "max",
            "runsPerDay": 2,
        },
        "catalogConsolidation": {
            "enabled": True,
            "cadenceDays": 7,
        },
        "recall": {
            "detailLevel": "compact",
            "timelineEnabled": True,
            "timelineMaxItems": 2,
        },
    },
    "context": {
        "recentInputBaseline": 20,
        "recentInputMaximum": 80,
        "recentInputCharMaximum": 12000,
        "axNodeMaximum": 160,
        "axCharMaximum": 12000,
        "tokenBudget": 4096,
        "reservedOutputTokens": 1024,
        "temporalRecall": True,
    },
    "planning": {
        "enabled": True,
        "injectIntoContext": True,
        "detectExplicitCompletion": True,
    },
    "agent": {
        "defaults": {
            "modelReference": "openai-codex/gpt-5.6-luna",
            "thinkingLevel": "max",
            "executionMode": "per_action",
        },
        "pi": {
            "enabled": False,
            "startup": "lazy",
            "idleTimeoutSeconds": 900,
            "systemProxy": True,
            "resumeLastSession": True,
            "defaultRoleId": "companion-future-v1",
            "toolProfile": "control-center-v1",
            "coordinatorEnabled": False,
        },
    },
    "voice": {
        "provider": "native_streaming",
        "hotkey": "middle_mouse",
        "hotwordsEnabled": False,
        "hotwords": [],
        "refinementModel": "inherit",
        "refinementThinkingLevel": "off",
    },
    "models": {
        "modelId": "",
        "hot": "minimind_ime_v2",
        "path": "",
        "promptMode": "base-completion",
        "maxTokens": 8,
        "temperature": 0.15,
        "topP": 0.85,
    },
    "activeRag": {
        "enabled": True,
        "quickModel": "deepseek/deepseek-v4-flash",
        "quickThinkingLevel": "high",
        "shortcut": "ctrl+.",
        "capture": {
            "accessibility": True,
            "clipboardFallback": True,
            "manualClipboardFallback": True,
        },
        "defaultIntent": "auto",
        "defaultPlacement": "replace_selection",
        "maxCandidates": 1,
        "latencyBudgetMs": 8000,
        "localOnlyDefault": True,
        "allowRemoteModel": True,
        "sensitiveTextGuard": True,
    },
    "knowledgeLibrary": {
        "parser": {
            "mineru": {
                "enabled": False,
                "port": 30001,
                "timeoutSeconds": 7200,
            },
        },
        "embedding": {
            "provider": "environment",
            "model": "",
            "baseUrl": "",
            "dimensions": 0,
            "secretReference": "",
            "queryPrefix": "",
            "documentPrefix": "",
            "denseBackend": "sqlite-exact",
        },
    },
    "pinyin": {
        "fuzzyProfile": "sichuan-mild",
        "rimeManagedPatch": True,
        "rerankUsesFuzzy": True,
        "pairs": {
            "zZh": True,
            "cCh": True,
            "sSh": True,
            "enEng": True,
            "inIng": True,
            "ongOn": True,
            "nL": False,
            "fH": False,
        },
    },
    "privacy": {
        "traceIncludeText": False,
        "debugIncludeText": False,
        "debugContextDirectory": "",
        "debugContextMaxGiB": 5,
        "debugContextMaxCallsPerTurn": 128,
        "redactEmails": True,
        "redactPaths": True,
        "redactSecrets": True,
        "blockSensitiveSelectedText": True,
        "allowRemoteModelForActiveRag": True,
        "allowRemoteModelForOfflineCompile": True,
        "retentionDays": 30,
    },
    "diagnostics": {
        "liveTrace": False,
        "dropStats": False,
        "candidateExplain": False,
    },
    "managementSecurity": {
        "requireToken": False,
        "sameOriginOnly": True,
        "postRequiresJson": True,
        "destructiveActionConfirmText": True,
    },
}


SETTINGS_SCHEMA: dict[str, object] = {
    "schemaVersion": "rag-ime.settings-schema.v3",
    "sections": [
        {
            "id": "identity",
            "label": "称呼与外观",
            "fields": [
                {
                    "key": "identity.productName",
                    "type": "string",
                    "label": "应用名称",
                    "default": "PAW",
                    "minLength": 1,
                    "maxLength": 24,
                },
                {
                    "key": "identity.assistantName",
                    "type": "string",
                    "label": "通用伙伴称呼",
                    "default": "Agent",
                    "minLength": 1,
                    "maxLength": 24,
                },
                {
                    "key": "identity.tagline",
                    "type": "string",
                    "label": "侧栏短句",
                    "default": "记得你，也陪你做事",
                    "minLength": 1,
                    "maxLength": 48,
                },
            ],
        },
        {
            "id": "interaction",
            "label": "Interaction",
            "fields": [
                {"key": "interaction.composition.showPrediction", "type": "boolean", "label": "输入拼音时显示 AI 候选", "default": False},
                {"key": "interaction.composition.showOnlyRime", "type": "boolean", "label": "输入拼音时只显示 Rime 候选", "default": True},
                {"key": "interaction.postCommit.showPendingStatus", "type": "boolean", "label": "预测开始时立即显示反馈", "default": False},
                {"key": "interaction.postCommit.enabled", "type": "boolean", "label": "启用提交后预测", "default": True},
                {"key": "interaction.postCommit.idleTriggerMs", "type": "integer", "label": "停顿触发时间", "default": 220},
                {"key": "interaction.postCommit.minDeltaChars", "type": "integer", "label": "最少新增字符数", "default": 2},
                {"key": "interaction.postCommit.maxCallsPer10s", "type": "integer", "label": "10 秒最大模型调用", "default": 6},
                {"key": "interaction.postCommit.cooldownMs", "type": "integer", "label": "空结果冷却", "default": 1500},
                {"key": "interaction.postCommit.pendingStatusDelayMs", "type": "integer", "label": "状态行延迟", "default": 600},
                {"key": "interaction.postCommit.panelTtlMs", "type": "integer", "label": "生成结果停留时间", "default": 4000},
                {"key": "interaction.postCommit.modelBudgetMs", "type": "integer", "label": "本机模型最长等待", "default": 900},
                {"key": "interaction.postCommit.numberKeys", "type": "enum", "label": "普通数字键", "options": ["pass_through"], "default": "pass_through"},
                {"key": "interaction.postCommit.tabAction", "type": "enum", "label": "Tab 行为", "options": ["accept_top_prediction", "rime_default", "disabled"], "default": "accept_top_prediction"},
                {"key": "interaction.postCommit.optionNumber", "type": "enum", "label": "Option+数字", "options": ["select_prediction_by_ordinal", "disabled"], "default": "select_prediction_by_ordinal"},
            ],
        },
        {
            "id": "display",
            "label": "Display",
            "fields": [
                {"key": "display.showSourceBadge", "type": "boolean", "label": "显示来源图标", "default": True},
                {"key": "display.showDiagnosticsInline", "type": "boolean", "label": "候选行内诊断", "default": False},
                {"key": "display.maxPostCommitCandidates", "type": "integer", "label": "Post-commit 候选数量", "default": 5},
                {"key": "display.badges.model", "type": "string", "label": "模型徽标", "default": "模"},
                {"key": "display.badges.rag", "type": "string", "label": "RAG 徽标", "default": "查"},
                {"key": "display.badges.memory", "type": "string", "label": "记忆徽标", "default": "忆"},
                {"key": "display.badges.status", "type": "string", "label": "状态行徽标", "default": "查忆"},
                {"key": "display.badges.action", "type": "string", "label": "手动生成按钮徽标", "default": "生成"},
                {"key": "display.panelStyle", "type": "enum", "label": "候选界面", "options": ["compact", "expanded"], "default": "compact"},
                {"key": "display.candidateFontSize", "type": "integer", "label": "候选字号", "default": 14},
                {"key": "display.fadeAnimation", "type": "boolean", "label": "淡入动画", "default": True},
                {"key": "display.maxWidth", "type": "integer", "label": "最大宽度", "default": 520},
            ],
        },
        {
            "id": "rag",
            "label": "RAG Core",
            "fields": [
                {"key": "rag.hybrid.enabled", "type": "boolean", "label": "启用 Hybrid RAG", "default": True},
                {"key": "rag.hybrid.budgetMs", "type": "integer", "label": "RAG 预算 ms", "default": 400},
                {"key": "rag.lanes.bm25Raw", "type": "boolean", "label": "原文 BM25（SQLite FTS5）", "default": True},
                {"key": "rag.lanes.bm25Tags", "type": "boolean", "label": "标签 BM25（SQLite FTS5）", "default": True},
                {"key": "rag.lanes.vectorRaw", "type": "boolean", "label": "向量召回", "default": True},
                {"key": "rag.lanes.tagMemo", "type": "boolean", "label": "TagMemo", "default": True},
                {"key": "rag.lanes.timeDailyBook", "type": "boolean", "label": "Time/Daily Book", "default": True},
            ],
        },
        {
            "id": "lexiconOrganization",
            "label": "词库定期整理",
            "fields": [
                {"key": "lexiconOrganization.enabled", "type": "boolean", "label": "启用定期整理", "default": True},
                {"key": "lexiconOrganization.runsPerDay", "type": "integer", "label": "每天整理次数", "default": 2},
            ],
        },
        {
            "id": "models",
            "label": "本机即时预测",
            "fields": [
                {"key": "models.modelId", "type": "string", "label": "注册模型 ID", "default": "", "maxLength": 128},
                {"key": "models.hot", "type": "enum", "label": "推理 Profile", "options": ["minimind_ime_v2", "qwen3_06b_ime_hot"], "default": "minimind_ime_v2"},
                {"key": "models.path", "type": "string", "label": "本机模型目录", "default": "", "maxLength": 1024},
                {"key": "models.promptMode", "type": "enum", "label": "Prompt 模式", "options": ["base-completion", "chat-json"], "default": "base-completion"},
                {"key": "models.maxTokens", "type": "integer", "label": "最大生成 Token", "default": 8},
                {"key": "models.temperature", "type": "number", "label": "Temperature", "default": 0.15},
                {"key": "models.topP", "type": "number", "label": "Top P", "default": 0.85},
            ],
        },
        {
            "id": "activeRag",
            "label": "Active RAG",
            "fields": [
                {"key": "activeRag.enabled", "type": "boolean", "label": "启用 Active RAG", "default": True},
                {
                    "key": "activeRag.quickModel",
                    "type": "pi-model",
                    "label": "闪电生成模型",
                    "default": "deepseek/deepseek-v4-flash",
                },
                {
                    "key": "activeRag.quickThinkingLevel",
                    "type": "pi-thinking",
                    "label": "闪电生成思考",
                    "default": "high",
                    "modelKey": "activeRag.quickModel",
                    "options": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
                },
                {"key": "activeRag.shortcut", "type": "shortcut", "label": "快捷键", "default": "ctrl+."},
                {"key": "activeRag.capture.accessibility", "type": "boolean", "label": "优先读取系统选区", "default": True},
                {"key": "activeRag.capture.clipboardFallback", "type": "boolean", "label": "显式触发允许剪贴板 fallback", "default": True},
                {"key": "activeRag.capture.manualClipboardFallback", "type": "boolean", "label": "允许手动剪贴板兜底", "default": True},
                {"key": "activeRag.defaultPlacement", "type": "enum", "label": "插入方式", "options": ["replace_selection", "insert_after_selection", "show_only"], "default": "replace_selection"},
                {"key": "activeRag.maxCandidates", "type": "integer", "label": "生成候选数量", "default": 1},
                {
                    "key": "activeRag.latencyBudgetMs",
                    "type": "integer",
                    "label": "生成框最长等待",
                    "default": 8000,
                },
                {"key": "activeRag.localOnlyDefault", "type": "boolean", "label": "预览默认仅使用本地 RAG", "default": True},
                {"key": "activeRag.allowRemoteModel", "type": "boolean", "label": "启用高质量生成", "default": True},
            ],
        },
        {
            "id": "memory",
            "label": "Memory",
            "fields": [
                {"key": "memory.enabled", "type": "boolean", "label": "启用记忆增强", "default": True},
                {"key": "memory.shortTermItems", "type": "integer", "label": "短期记忆条数", "default": 20},
                {"key": "memory.shortTermTtlMinutes", "type": "integer", "label": "短期记忆 TTL", "default": 30},
                {"key": "memory.allowProjectFallback", "type": "boolean", "label": "允许项目级回退", "default": True},
                {"key": "memory.allowAppFallback", "type": "boolean", "label": "允许 App 级回退", "default": True},
                {"key": "memory.allowGlobalMemory", "type": "boolean", "label": "允许全局记忆", "default": True},
                {"key": "memory.archiveInactiveDays", "type": "integer", "label": "主题书闲置归档天数", "default": 60},
                {"key": "memory.timeDecay.temporaryHalfLifeDays", "type": "integer", "label": "临时事项衰减半衰期", "default": 14},
                {"key": "memory.timeDecay.projectHalfLifeDays", "type": "integer", "label": "项目事实衰减半衰期", "default": 120},
                {"key": "memory.timeDecay.topicBookHalfLifeDays", "type": "integer", "label": "主题书衰减半衰期", "default": 180},
                {"key": "memory.timeDecay.stablePreferenceHalfLifeDays", "type": "integer", "label": "稳定偏好衰减半衰期", "default": 365},
                {"key": "memory.automaticOrganization.enabled", "type": "boolean", "label": "自动整理", "default": True},
                {"key": "memory.automaticOrganization.model", "type": "pi-model", "label": "自动整理模型", "default": "openai-codex/gpt-5.6-luna"},
                {"key": "memory.automaticOrganization.thinkingLevel", "type": "pi-thinking", "label": "自动整理思考", "default": "max", "modelKey": "memory.automaticOrganization.model", "options": ["off", "minimal", "low", "medium", "high", "xhigh", "max"]},
                {"key": "memory.automaticOrganization.runsPerDay", "type": "integer", "label": "每天自动整理次数", "default": 2},
                {"key": "memory.automaticOrganization.includeAgentDialogue", "type": "boolean", "label": "整理 Agent 对话摘要", "default": True},
                {"key": "memory.dreaming.enabled", "type": "boolean", "label": "记忆做梦", "default": True},
                {"key": "memory.dreaming.model", "type": "pi-model", "label": "做梦模型", "default": "openai-codex/gpt-5.6-luna"},
                {"key": "memory.dreaming.thinkingLevel", "type": "pi-thinking", "label": "做梦思考", "default": "max", "modelKey": "memory.dreaming.model", "options": ["off", "minimal", "low", "medium", "high", "xhigh", "max"]},
                {"key": "memory.dreaming.runsPerDay", "type": "integer", "label": "每天做梦次数", "default": 2},
                {"key": "memory.catalogConsolidation.enabled", "type": "boolean", "label": "启用全局目录整理", "default": True},
                {"key": "memory.catalogConsolidation.cadenceDays", "type": "integer", "label": "全局目录整理周期（天）", "default": 7},
                {"key": "memory.recall.detailLevel", "type": "enum", "label": "召回详细程度", "options": ["compact", "balanced", "detailed"], "default": "compact"},
                {"key": "memory.recall.timelineEnabled", "type": "boolean", "label": "按需召回时间线", "default": True},
                {"key": "memory.recall.timelineMaxItems", "type": "integer", "label": "时间线最多召回条数", "default": 2},
            ],
        },
        {
            "id": "knowledgeLibrary",
            "label": "文档知识库",
            "fields": [
                {
                    "key": "knowledgeLibrary.parser.mineru.enabled",
                    "type": "boolean",
                    "label": "启用本机 MinerU",
                    "default": False,
                },
                {
                    "key": "knowledgeLibrary.parser.mineru.port",
                    "type": "integer",
                    "label": "MinerU 本机端口",
                    "default": 30001,
                },
                {
                    "key": "knowledgeLibrary.parser.mineru.timeoutSeconds",
                    "type": "integer",
                    "label": "MinerU 单文档最长等待（秒）",
                    "default": 7200,
                },
                {
                    "key": "knowledgeLibrary.embedding.provider",
                    "type": "enum",
                    "label": "Embedding Provider",
                    "options": [
                        "environment",
                        "none",
                        "local-hash",
                        "sentence-transformers",
                        "mlx-bert",
                        "openai-compatible",
                    ],
                    "default": "environment",
                },
                {
                    "key": "knowledgeLibrary.embedding.model",
                    "type": "string",
                    "label": "Embedding 模型",
                    "default": "",
                    "maxLength": 1000,
                },
                {
                    "key": "knowledgeLibrary.embedding.baseUrl",
                    "type": "string",
                    "label": "兼容 API 地址",
                    "default": "",
                    "maxLength": 2000,
                },
                {
                    "key": "knowledgeLibrary.embedding.dimensions",
                    "type": "integer",
                    "label": "向量维度",
                    "default": 0,
                },
                {
                    "key": "knowledgeLibrary.embedding.secretReference",
                    "type": "string",
                    "label": "密钥引用",
                    "default": "",
                    "maxLength": 128,
                },
                {
                    "key": "knowledgeLibrary.embedding.queryPrefix",
                    "type": "string",
                    "label": "Query Prefix",
                    "default": "",
                    "maxLength": 500,
                },
                {
                    "key": "knowledgeLibrary.embedding.documentPrefix",
                    "type": "string",
                    "label": "Document Prefix",
                    "default": "",
                    "maxLength": 500,
                },
                {
                    "key": "knowledgeLibrary.embedding.denseBackend",
                    "type": "enum",
                    "label": "Dense 索引后端",
                    "options": ["sqlite-exact", "usearch"],
                    "default": "sqlite-exact",
                },
            ],
        },
        {
            "id": "context",
            "label": "上下文",
            "fields": [
                {"key": "context.recentInputBaseline", "type": "integer", "label": "最近完整输入基线", "default": 20},
                {"key": "context.recentInputMaximum", "type": "integer", "label": "最近完整输入条数上限", "default": 80},
                {"key": "context.recentInputCharMaximum", "type": "integer", "label": "最近输入字符预算", "default": 12000},
                {"key": "context.axNodeMaximum", "type": "integer", "label": "AX 节点数预算", "default": 160},
                {"key": "context.axCharMaximum", "type": "integer", "label": "AX 字符预算", "default": 12000},
                {"key": "context.tokenBudget", "type": "integer", "label": "上下文 token 预算", "default": 4096},
                {"key": "context.reservedOutputTokens", "type": "integer", "label": "预留输出 token", "default": 1024},
                {"key": "context.temporalRecall", "type": "boolean", "label": "识别昨天、上周等时间表达", "default": True},
            ],
        },
        {
            "id": "planning",
            "label": "任务与规划",
            "fields": [
                {"key": "planning.enabled", "type": "boolean", "label": "启用规划与任务", "default": True},
                {"key": "planning.injectIntoContext", "type": "boolean", "label": "将今日计划注入上下文", "default": True},
                {"key": "planning.detectExplicitCompletion", "type": "boolean", "label": "从明确表达识别任务完成", "default": True},
            ],
        },
        {
            "id": "agent",
            "label": "Agent 运行时",
            "fields": [
                {
                    "key": "agent.defaults.modelReference",
                    "type": "pi-model-or-inherit",
                    "label": "新建 Session 模型",
                    "default": "openai-codex/gpt-5.6-luna",
                },
                {
                    "key": "agent.defaults.thinkingLevel",
                    "type": "pi-thinking",
                    "label": "新建 Session 推理强度",
                    "default": "max",
                    "modelKey": "agent.defaults.modelReference",
                    "options": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
                },
                {
                    "key": "agent.defaults.executionMode",
                    "type": "enum",
                    "label": "新建 Session 执行权限",
                    "default": "per_action",
                    "options": ["read_only", "per_action", "workspace_managed", "full_trust"],
                },
                {"key": "agent.pi.enabled", "type": "boolean", "label": "连接 Pi", "default": False},
                {"key": "agent.pi.idleTimeoutSeconds", "type": "integer", "label": "空闲退出时间", "default": 900},
                {"key": "agent.pi.systemProxy", "type": "boolean", "label": "远程模型跟随系统代理", "default": True},
                {"key": "agent.pi.resumeLastSession", "type": "boolean", "label": "恢复上次对话", "default": True},
            ],
        },
        {
            "id": "voice",
            "label": "语音输入",
            "fields": [
                {"key": "voice.provider", "type": "enum", "label": "语音转写引擎", "options": ["native_streaming", "realtime_websocket", "http_transcription"], "default": "native_streaming"},
                {"key": "voice.hotkey", "type": "enum", "label": "按住说话", "options": ["middle_mouse", "right_option", "option_space"], "default": "middle_mouse"},
                {"key": "voice.hotwordsEnabled", "type": "boolean", "label": "启用语音热词", "default": False},
                {"key": "voice.hotwords", "type": "string-list", "label": "语音热词", "default": []},
                {
                    "key": "voice.refinementModel",
                    "type": "pi-model-or-inherit",
                    "label": "保守校对模型",
                    "default": "inherit",
                },
                {
                    "key": "voice.refinementThinkingLevel",
                    "type": "pi-thinking",
                    "label": "保守校对思考",
                    "default": "off",
                    "modelKey": "voice.refinementModel",
                    "options": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
                },
            ],
        },
        {
            "id": "pinyin",
            "label": "Pinyin",
            "fields": [
                {"key": "pinyin.fuzzyProfile", "type": "enum", "label": "模糊音配置", "options": ["sichuan-mild", "none"], "default": "sichuan-mild"},
                {"key": "pinyin.rimeManagedPatch", "type": "boolean", "label": "安装 Rime 模糊音 patch", "default": True},
                {"key": "pinyin.rerankUsesFuzzy", "type": "boolean", "label": "RAG/词库重排使用模糊音", "default": True},
                {"key": "pinyin.pairs.zZh", "type": "boolean", "label": "z/zh", "default": True},
                {"key": "pinyin.pairs.cCh", "type": "boolean", "label": "c/ch", "default": True},
                {"key": "pinyin.pairs.sSh", "type": "boolean", "label": "s/sh", "default": True},
                {"key": "pinyin.pairs.enEng", "type": "boolean", "label": "en/eng", "default": True},
                {"key": "pinyin.pairs.inIng", "type": "boolean", "label": "in/ing", "default": True},
                {"key": "pinyin.pairs.ongOn", "type": "boolean", "label": "on/ong（漏 g）", "default": True},
                {"key": "pinyin.pairs.nL", "type": "boolean", "label": "n/l", "default": False},
                {"key": "pinyin.pairs.fH", "type": "boolean", "label": "f/h", "default": False},
            ],
        },
        {
            "id": "privacy",
            "label": "Privacy",
            "fields": [
                {"key": "privacy.debugIncludeText", "type": "boolean", "label": "保存并查看本机上下文快照", "default": False},
                {
                    "key": "privacy.debugContextDirectory",
                    "type": "string",
                    "label": "上下文快照目录（支持外置硬盘）",
                    "default": "",
                    "maxLength": 1024,
                },
                {"key": "privacy.debugContextMaxGiB", "type": "integer", "label": "上下文快照容量", "default": 5},
                {"key": "privacy.debugContextMaxCallsPerTurn", "type": "integer", "label": "每回合保留的模型调用", "default": 128},
                {"key": "privacy.traceIncludeText", "type": "boolean", "label": "trace 包含原文", "default": False},
                {"key": "privacy.allowRemoteModelForActiveRag", "type": "boolean", "label": "显式生成可使用联网模型", "default": True},
                {"key": "managementSecurity.requireToken", "type": "boolean", "label": "POST 需要管理 token", "default": False},
            ],
        },
    ],
}


SENSITIVE_SETTING_SUFFIXES = ("apiKey", "api_key", "token", "secret", "password")


def default_settings() -> dict[str, object]:
    return copy.deepcopy(DEFAULT_SETTINGS)


def settings_schema() -> dict[str, object]:
    schema = copy.deepcopy(SETTINGS_SCHEMA)
    for section in schema["sections"]:  # type: ignore[index]
        for field in section.get("fields", []):
            key = str(field.get("key") or "")
            field_type = str(field.get("type") or "string")
            metadata = _FIELD_METADATA.get(key, {})
            field.setdefault("description", metadata.get("description", str(field.get("label") or key)))
            field.setdefault("applyMode", metadata.get("applyMode", "live"))
            field.setdefault("risk", metadata.get("risk", "safe"))
            field.setdefault("expert", metadata.get("expert", key.startswith("rag.weights.")))
            field.setdefault("min", metadata.get("min"))
            field.setdefault("max", metadata.get("max"))
            field.setdefault("step", metadata.get("step", 1 if field_type == "integer" else None))
            field.setdefault("unit", metadata.get("unit", ""))
            field.setdefault("validation", metadata.get("validation", {}))
            field.setdefault("restartComponent", metadata.get("restartComponent", ""))
    return schema


_FIELD_METADATA: dict[str, dict[str, object]] = {
    "identity.productName": {
        "description": "显示在侧栏和窗口标题中；不会改变安装包文件名",
    },
    "identity.assistantName": {
        "description": "没有指向某位具体伙伴时使用；自建伙伴可以单独命名，内置伙伴复制后也能调整",
    },
    "identity.tagline": {
        "description": "应用名称下方的一句短介绍",
    },
    "interaction.postCommit.idleTriggerMs": {"description": "连续输入合并后等待多久触发预测", "min": 40, "max": 1000, "step": 20, "unit": "ms", "applyMode": "restart_input_method", "restartComponent": "squirrel"},
    "interaction.postCommit.minDeltaChars": {"description": "相较上次预测至少新增的字符数", "min": 1, "max": 32, "unit": "字符"},
    "interaction.postCommit.maxCallsPer10s": {"description": "限制连续输入期间的模型调用预算", "min": 0, "max": 10, "unit": "次"},
    "interaction.postCommit.cooldownMs": {"description": "空结果后再次调用模型前的等待时间", "min": 0, "max": 10000, "step": 100, "unit": "ms"},
    "interaction.postCommit.panelTtlMs": {"description": "生成结果出现后自动关闭前的停留时间；不会缩短正在生成状态", "min": 500, "max": 30000, "step": 500, "unit": "ms", "applyMode": "restart_input_method", "restartComponent": "squirrel"},
    "interaction.postCommit.modelBudgetMs": {"description": "一次本机预测最多等待多久；过期结果不会进入候选面板", "min": 300, "max": 12000, "step": 100, "unit": "ms"},
    "interaction.postCommit.numberKeys": {"description": "普通数字键始终交给 Rime 或当前应用"},
    "interaction.postCommit.optionNumber": {"description": "Option+数字按候选序号选择智能候选"},
    "display.maxPostCommitCandidates": {"min": 1, "max": 8, "unit": "项", "applyMode": "restart_input_method", "restartComponent": "squirrel"},
    "display.candidateFontSize": {"min": 11, "max": 24, "unit": "pt", "applyMode": "restart_input_method", "restartComponent": "squirrel"},
    "display.maxWidth": {"min": 320, "max": 760, "step": 20, "unit": "pt", "applyMode": "restart_input_method", "restartComponent": "squirrel"},
    "display.badges.model": {"expert": True},
    "display.badges.rag": {"expert": True},
    "display.badges.memory": {"expert": True},
    "display.badges.status": {"expert": True},
    "display.badges.action": {"expert": True},
    "activeRag.shortcut": {"description": "显式生成快捷键", "applyMode": "restart_input_method", "restartComponent": "squirrel"},
    "activeRag.quickModel": {"description": "闪电按钮每次从 Pi 实时目录校验并调用的单次回复模型"},
    "activeRag.quickThinkingLevel": {
        "description": (
            "闪电生成默认使用高思考以保证指令理解和输出正确性；"
            "关闭思考仅作为专家级低延迟覆盖，可能降低结果质量。"
            "该请求不创建 Agent Session，也不加载工具"
        )
    },
    "activeRag.latencyBudgetMs": {
        "description": "显式生成仍在准备或生成时，等待框自动收起前的最长时间",
        "min": 2000,
        "max": 30000,
        "step": 1000,
        "unit": "ms",
    },
    "activeRag.allowRemoteModel": {"description": "只允许显式 Active RAG 使用远程模型", "risk": "sensitive", "validation": {"confirmText": "ALLOW REMOTE MODEL"}},
    "knowledgeLibrary.parser.mineru.enabled": {
        "description": "只连接本机 loopback MinerU 解析服务；不会启动命令或使用云端 API",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
    },
    "knowledgeLibrary.parser.mineru.port": {
        "description": "MinerU loopback HTTP 端口，主机和路径由服务端固定",
        "min": 1024,
        "max": 65535,
        "unit": "端口",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
    },
    "knowledgeLibrary.parser.mineru.timeoutSeconds": {
        "description": "本机 MinerU 单个文档的最长解析等待时间；超时会保留可重试错误，不会伪装成服务未安装",
        "min": 60,
        "max": 86400,
        "unit": "秒",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
    },
    "knowledgeLibrary.embedding.provider": {
        "description": "environment 保持现有环境配置；其他选项保存一个知识库专用、无明文密钥的活动 Profile",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
    },
    "knowledgeLibrary.embedding.model": {
        "description": "本机模型目录或远程兼容服务的模型 ID；应用前应先完成 Probe",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
    },
    "knowledgeLibrary.embedding.baseUrl": {
        "description": "仅 openai-compatible 使用的 HTTP(S) 服务地址；不含密钥",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
    },
    "knowledgeLibrary.embedding.dimensions": {
        "description": "0 表示由模型报告；显式值会在 Probe 时校验",
        "min": 0,
        "max": 65536,
        "unit": "维",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
    },
    "knowledgeLibrary.embedding.secretReference": {
        "description": "只保存环境变量名称，绝不保存或回显 API Key",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
        "expert": True,
    },
    "knowledgeLibrary.embedding.queryPrefix": {
        "description": "模型要求时添加到查询文本前；会改变 Provider fingerprint",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
        "expert": True,
    },
    "knowledgeLibrary.embedding.documentPrefix": {
        "description": "模型要求时添加到文档文本前；会改变 Provider fingerprint",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
        "expert": True,
    },
    "knowledgeLibrary.embedding.denseBackend": {
        "description": "sqlite-exact 适合小库与基准；usearch 需要已安装可选依赖",
        "applyMode": "restart_knowledge_worker",
        "restartComponent": "knowledge-worker",
        "expert": True,
    },
    "agent.pi.enabled": {"description": "按需启动受管理的 Pi RPC，不影响普通输入路径"},
    "agent.defaults.modelReference": {
        "description": "新建 Session 跟随的 Pi Provider 模型；当前 Session 仍可在 Composer 中单独调整",
        "applyMode": "next_session",
    },
    "agent.defaults.thinkingLevel": {
        "description": "新建 Session 使用的思考档位；只允许所选模型明确支持的值",
        "applyMode": "next_session",
    },
    "agent.defaults.executionMode": {
        "description": "新建 Session 的执行权限起点；高风险动作仍服从产品审批与 Runtime policy",
        "applyMode": "next_session",
    },
    "agent.pi.idleTimeoutSeconds": {"description": "Pi 无活动后自动退出的等待时间", "min": 0, "max": 86400, "step": 60, "unit": "秒"},
    "agent.pi.systemProxy": {
        "description": "OpenAI 等远程 Provider 默认读取 macOS 的 HTTP/HTTPS 系统代理；localhost 与本机服务始终直连",
        "applyMode": "restart_agent_gateway",
        "restartComponent": "agent-gateway",
    },
    "voice.provider": {"description": "选择下一次听写连接使用的转写引擎或兼容 API", "applyMode": "next_voice_session", "restartComponent": "voice"},
    "voice.hotkey": {"description": "选择全局按住说话快捷键", "applyMode": "next_voice_session", "restartComponent": "voice"},
    "voice.hotwordsEnabled": {"description": "仅在豆包/火山原生流式识别请求中发送已确认的热词", "applyMode": "next_voice_session", "restartComponent": "voice"},
    "voice.hotwords": {"description": "每行一个中英文或技术词，最多 32 个，每个 2 至 9 个字符", "applyMode": "next_voice_session", "restartComponent": "voice"},
    "voice.refinementModel": {
        "description": "语音识别结束后的保守文字校对模型；跟随默认时使用当前 Agent 默认模型",
        "applyMode": "live",
    },
    "voice.refinementThinkingLevel": {
        "description": "只影响语音校对的独立无工具 Session；默认关闭思考以降低定稿等待",
        "applyMode": "live",
    },
    "pinyin.rimeManagedPatch": {"description": "写入受管理的 Rime 模糊音 patch", "applyMode": "redeploy_rime", "restartComponent": "rime"},
    "pinyin.fuzzyProfile": {"applyMode": "redeploy_rime", "restartComponent": "rime"},
    "models.modelId": {
        "description": "选择 models.json 中已经登记的本机模型；留空时沿用当前 hot 模型",
        "applyMode": "restart_predictor",
        "restartComponent": "predictor",
    },
    "models.hot": {
        "description": "本机模型的推理契约；它不属于 Pi Provider 模型",
        "applyMode": "restart_predictor",
        "restartComponent": "predictor",
    },
    "lexiconOrganization.enabled": {
        "description": "由现有本机维护轮询定期汇总 Rime 真实选词反馈；只生成可审阅建议，不写入或重排 Rime 候选",
        "applyMode": "next_maintenance_run",
    },
    "lexiconOrganization.runsPerDay": {
        "description": "现有本机维护轮询按此频率判断是否到期，不创建第二个计时器",
        "applyMode": "next_maintenance_run",
        "min": 1,
        "max": 6,
        "unit": "次/天",
    },
    "models.path": {
        "description": "已登记模型的本机绝对目录；留空时沿用注册表目录",
        "applyMode": "restart_predictor",
        "restartComponent": "predictor",
        "risk": "sensitive",
    },
    "models.promptMode": {
        "description": "基础续写模型使用 Base Completion；对话模型使用 Chat JSON",
        "applyMode": "restart_predictor",
        "restartComponent": "predictor",
    },
    "models.maxTokens": {
        "description": "每个短候选允许生成的最大 Token 数",
        "min": 1,
        "max": 64,
        "unit": "token",
        "applyMode": "restart_predictor",
        "restartComponent": "predictor",
    },
    "models.temperature": {
        "description": "候选采样随机度；越低越稳定",
        "min": 0.0,
        "max": 2.0,
        "step": 0.05,
        "applyMode": "restart_predictor",
        "restartComponent": "predictor",
    },
    "models.topP": {
        "description": "候选核采样范围",
        "min": 0.05,
        "max": 1.0,
        "step": 0.05,
        "applyMode": "restart_predictor",
        "restartComponent": "predictor",
    },
    "privacy.traceIncludeText": {"risk": "sensitive", "expert": True},
    "privacy.debugIncludeText": {
        "description": "开启后保存可供上下文检查恢复的 Provider/Tool 快照；凭证、隐藏推理和二进制正文会先脱敏，关闭后只保留当前 Runtime 内存",
        "applyMode": "restart_agent_gateway",
        "restartComponent": "agent-gateway",
        "risk": "sensitive",
        "expert": False,
    },
    "privacy.debugContextDirectory": {
        "description": "填写绝对路径或 ~/ 路径；建议放在外置硬盘。留空时使用应用支持目录，磁盘未挂载会显示写入错误而不会改写其他目录",
        "applyMode": "restart_agent_gateway",
        "restartComponent": "agent-gateway",
        "risk": "sensitive",
        "expert": False,
    },
    "privacy.debugContextMaxGiB": {
        "description": "所有会话快照共用的目录上限；达到上限后按修改时间删除最旧文件",
        "applyMode": "restart_agent_gateway",
        "restartComponent": "agent-gateway",
        "min": 1,
        "max": 64,
        "unit": "GiB",
        "expert": False,
    },
    "privacy.debugContextMaxCallsPerTurn": {
        "description": "长工具链、重试和继续执行时，每个回合最多保留多少次模型调用",
        "applyMode": "restart_agent_gateway",
        "restartComponent": "agent-gateway",
        "min": 1,
        "max": 256,
        "unit": "次/回合",
        "expert": True,
    },
    "managementSecurity.requireToken": {"applyMode": "restart_sidecar", "restartComponent": "sidecar", "expert": True},
    "memory.archiveInactiveDays": {"min": 7, "max": 3650, "unit": "天"},
    "memory.timeDecay.temporaryHalfLifeDays": {"min": 1, "max": 365, "unit": "天", "expert": True},
    "memory.timeDecay.projectHalfLifeDays": {"min": 7, "max": 1825, "unit": "天", "expert": True},
    "memory.timeDecay.topicBookHalfLifeDays": {"min": 7, "max": 3650, "unit": "天", "expert": True},
    "memory.timeDecay.stablePreferenceHalfLifeDays": {"min": 30, "max": 3650, "unit": "天", "expert": True},
    "memory.automaticOrganization.enabled": {
        "description": "把 Agent 在对话中通过 memory_capture 标记的用户长期候选异步编译为 Atom 与 Topic Book；原始会话、工具回执和任务状态不参与",
        "applyMode": "next_maintenance_run",
    },
    "memory.automaticOrganization.model": {
        "description": "自动整理下一次使用的 Pi Provider 模型；模型目录不可用时拒绝执行，不静默改用其他模型",
        "applyMode": "next_maintenance_run",
    },
    "memory.automaticOrganization.thinkingLevel": {
        "description": "自动整理使用所选 Pi 模型支持的思考档位；不支持时拒绝执行",
        "applyMode": "next_maintenance_run",
    },
    "memory.automaticOrganization.runsPerDay": {
        "description": "后台按此频率判断新来源并自动应用治理后的低风险记忆变更",
        "applyMode": "next_maintenance_run",
        "min": 1,
        "max": 6,
        "unit": "次/天",
    },
    "memory.automaticOrganization.includeAgentDialogue": {
        "description": "只传入最新会话摘要和摘要后的有界短尾窗，作为不可独立支持事实的上下文；关闭后不删除任何 Evidence、Atom 或 Book",
        "applyMode": "next_maintenance_run",
    },
    "memory.dreaming.enabled": {
        "description": "周期性跨 Session 压缩近期工作、关系和角色连续性；关闭后不会删除已生成产物",
        "applyMode": "next_maintenance_run",
    },
    "memory.dreaming.model": {
        "description": "做梦下一次使用的 Pi Provider 模型；模型目录不可用时拒绝执行，不静默改用其他模型",
        "applyMode": "next_maintenance_run",
    },
    "memory.dreaming.thinkingLevel": {
        "description": "做梦使用所选 Pi 模型支持的思考档位；不支持时拒绝执行",
        "applyMode": "next_maintenance_run",
    },
    "memory.dreaming.runsPerDay": {
        "description": "后台做梦的逻辑频率；系统会轻量轮询，只有到期才调用模型",
        "applyMode": "next_maintenance_run",
        "min": 1,
        "max": 6,
        "unit": "次/天",
    },
    "memory.catalogConsolidation.enabled": {
        "description": "每周对完整的 governed Memory catalog 做一次保守合并审查；只允许合并精确等价 Atom 与同义 Tag，不创建、修改或删除事实、Topic Book、Group、Tag edge 或嵌套成员",
        "applyMode": "next_maintenance_run",
    },
    "memory.catalogConsolidation.cadenceDays": {
        "description": "全局 Memory catalog 整理周期；Gateway 每小时只触发一次到期判断，真正执行由持久化回执决定",
        "applyMode": "next_maintenance_run",
        "min": 1,
        "max": 365,
        "unit": "天",
    },
    "memory.recall.detailLevel": {
        "description": "compact 只注入主题摘要和最相关 Atom；balanced 与 detailed 逐步放宽片段预算",
        "applyMode": "next_request",
    },
    "memory.recall.timelineEnabled": {
        "description": "仅在统一 TimelineIntent 判定命中明确日期、相对时间或时间线表达时开放 Timeline 通道",
        "applyMode": "next_request",
    },
    "memory.recall.timelineMaxItems": {
        "description": "一次问题最多注入的独立 Timeline 片段数量",
        "applyMode": "next_request",
        "min": 1,
        "max": 4,
        "unit": "条",
    },
    "context.recentInputBaseline": {"min": 10, "max": 80, "unit": "条"},
    "context.recentInputMaximum": {"min": 20, "max": 200, "unit": "条"},
    "context.recentInputCharMaximum": {"min": 256, "max": 100000, "unit": "字符"},
    "context.axNodeMaximum": {"min": 1, "max": 160, "unit": "节点"},
    "context.axCharMaximum": {"min": 256, "max": 12000, "unit": "字符"},
    "context.tokenBudget": {"min": 2048, "max": 32768, "step": 512, "unit": "token"},
    "context.reservedOutputTokens": {"min": 256, "max": 8192, "step": 256, "unit": "token"},
}


def deep_merge_settings(base: Mapping[str, object], updates: Mapping[str, object]) -> dict[str, object]:
    result = copy.deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge_settings(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = copy.deepcopy(value)
    return result


def flatten_settings(value: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            result.update(flatten_settings(item, full_key))
        else:
            result[full_key] = item
    return result


def unflatten_settings(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        parts = str(key).split(".")
        cursor = result
        for part in parts[:-1]:
            existing = cursor.get(part)
            if not isinstance(existing, dict):
                cursor[part] = {}
            cursor = cursor[part]  # type: ignore[assignment]
        cursor[parts[-1]] = item
    return result


def section_defaults(section: str) -> dict[str, object]:
    value = DEFAULT_SETTINGS.get(section)
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def redact_settings(settings: Mapping[str, object]) -> dict[str, object]:
    redacted = copy.deepcopy(dict(settings))
    flat = flatten_settings(redacted)
    for key in list(flat):
        lowered = key.lower()
        if any(lowered.endswith(suffix.lower()) for suffix in SENSITIVE_SETTING_SUFFIXES):
            flat[key] = "<redacted>"
    return unflatten_settings(flat)


def stable_settings_hash(settings: Mapping[str, object]) -> str:
    body = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
