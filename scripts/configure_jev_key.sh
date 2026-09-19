#!/usr/bin/env bash
set -euo pipefail

SERVICE="com.rag-ime.model-provider"
ACCOUNT="typesafe-api-key"
if ! command -v security >/dev/null 2>&1; then
  echo "macOS security 命令不可用。也可以通过 LaunchAgent 环境变量 TYPESAFE_API_KEY 提供密钥。" >&2
  exit 2
fi
printf 'TypeSafe/Jev API key（输入不回显，回车后写入 macOS Keychain）： '
IFS= read -r -s KEY
printf '\n'
if [[ -z "$KEY" ]]; then
  echo "未写入空密钥。" >&2
  exit 2
fi
security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT" -w "$KEY" >/dev/null
unset KEY
echo "Jev key 已写入 Keychain（服务=$SERVICE，账号=$ACCOUNT）；不会写入仓库、Session 或审批回执。"
