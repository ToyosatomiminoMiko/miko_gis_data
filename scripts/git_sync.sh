#!/usr/bin/env bash
# 技能仓自演进提交器: 提交当前改动并推送到 GitHub.
#
# 用法: bash scripts/git_sync.sh "<type>: <一句话>"
#   type: feat | fix | docs | example
#
# 为什么需要它:
#   1) 先按 git 配置的代理推送; 代理未启动(127.0.0.1:7897 常见)时自动绕过代理直连重试.
#   2) 统一"工作区干净就跳过", 避免为提交而提交.
#   3) 推送失败时保留本地提交并打印重试命令, 不丢演进成果.
set -uo pipefail

msg="${1:-}"
if [ -z "$msg" ]; then
  echo "用法: bash scripts/git_sync.sh \"<feat|fix|docs|example>: 一句话\"" >&2
  exit 1
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root" || exit 1

if [ -z "$(git status --porcelain)" ]; then
  echo "工作区干净: 本次没有可沉淀的改动, 跳过提交."
  exit 0
fi

git add -A && git commit -m "$msg" || exit 1
echo "已提交: $(git rev-parse --short HEAD) $msg"

pushed=0
if git push 2>/dev/null; then
  pushed=1
elif git -c http.proxy= -c https.proxy= push 2>/dev/null; then
  pushed=1
fi

if [ "$pushed" = 1 ]; then
  echo "已推送: $(git rev-parse --short HEAD)"
else
  echo "推送失败: 本地提交已保留. 网络恢复后执行:" >&2
  echo "  git push   # 或 git -c http.proxy= -c https.proxy= push" >&2
  echo "  若提示 non-fast-forward, 先 git pull --rebase 再重试." >&2
  exit 2
fi
