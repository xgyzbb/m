#!/bin/zsh
set -u

# 唯一真实副本（TCC 安全、无中文，launchd 可读）。Documents/自动监控 是指向这里的软链接。
MONITOR_DIR="/Users/bill/Library/Application Support/giftcard-monitor"
PYTHON="$MONITOR_DIR/.venv-mac/bin/python"
SCRIPT="$MONITOR_DIR/run_local.py"
LOCK_DIR="$MONITOR_DIR/.mac-launchd.lock"
WRAPPER_LOG="$MONITOR_DIR/run_local_launchd.log"
HEARTBEAT="$MONITOR_DIR/.heartbeat"

timestamp() { date "+%Y-%m-%d %H:%M:%S %Z"; }

# 防重入：上一轮还没跑完就跳过
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(timestamp) previous run still active; skipped" >> "$WRAPPER_LOG"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

[ -x "$PYTHON" ] || { echo "$(timestamp) missing python: $PYTHON" >> "$WRAPPER_LOG"; exit 2; }
[ -f "$SCRIPT" ] || { echo "$(timestamp) missing script: $SCRIPT" >> "$WRAPPER_LOG"; exit 2; }

cd "$MONITOR_DIR" || exit 2
echo "$(timestamp) start" >> "$WRAPPER_LOG"
"$PYTHON" "$SCRIPT" >> "$WRAPPER_LOG" 2>&1
code=$?
echo "$(timestamp) exit=$code" >> "$WRAPPER_LOG"
# 成功跑完才更新心跳（供看门狗判断监控是否还活着）
[ "$code" -eq 0 ] && date +%s > "$HEARTBEAT"
exit "$code"
