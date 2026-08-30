#!/usr/bin/env bash
# 便携版入口：自动定位脚本目录
# Python 探测顺序：BILI_PYTHON 环境变量 → python3 → python
# （Linux/macOS 上 python 常不存在，只有 python3，不能只写 python）
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pick_python() {
  if [ -n "${BILI_PYTHON:-}" ] && command -v "$BILI_PYTHON" >/dev/null 2>&1; then
    echo "$BILI_PYTHON"; return 0
  fi
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return 0; fi
  done
  echo ""
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  echo "[错误] 未找到 Python 解释器。请安装 Python 3.10+，或用 BILI_PYTHON 指定路径。" >&2
  exit 1
fi

export PYTHONPATH=
export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_DISABLE_SYMLINKS=1
export HF_HUB_DISABLE_XET=1
exec "$PY" "$HERE/bili_asr.py" "$@"
