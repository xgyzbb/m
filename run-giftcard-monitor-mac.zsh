#!/bin/zsh
set -u

# 唯一真实副本（TCC 安全、无中文，launchd 可读）。Documents/自动监控 是指向这里的软链接。
MONITOR_DIR="/Users/bill/Library/Application Support/giftcard-monitor"
PYTHON="$MONITOR_DIR/.venv-mac/bin/python"
SCRIPT="$MONITOR_DIR/run_local.py"
LOCK_DIR="$MONITOR_DIR/.mac-launchd.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
STALE_AGE=600     # 秒：锁超过 10 分钟必属僵死（正常一轮 < 1 分钟），可强制回收
WRAPPER_LOG="$MONITOR_DIR/run_local_launchd.log"
HEARTBEAT="$MONITOR_DIR/.heartbeat"

timestamp() { date "+%Y-%m-%d %H:%M:%S %Z"; }

# 防重入：上一轮还没跑完就跳过。但若锁是“僵死锁”（持有进程已死，
# 或锁存在超过 STALE_AGE——多由系统休眠/强杀导致 trap 未清理）则回收，避免永久卡死。
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_PID_FILE"; return 0
  fi
  local lpid lmtime lage
  lpid=$(cat "$LOCK_PID_FILE" 2>/dev/null)
  lmtime=$(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)
  lage=$(( $(date +%s) - lmtime ))
  # 仅当持有进程仍存活且锁未超时，才认定上一轮真的在跑 → 跳过
  if [ -n "$lpid" ] && kill -0 "$lpid" 2>/dev/null && [ "$lage" -lt "$STALE_AGE" ]; then
    return 1
  fi
  echo "$(timestamp) stale lock reclaimed (pid=${lpid:-none}, age=${lage}s)" >> "$WRAPPER_LOG"
  rm -rf "$LOCK_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_PID_FILE"; return 0
  fi
  return 1
}

if ! acquire_lock; then
  echo "$(timestamp) previous run still active; skipped" >> "$WRAPPER_LOG"
  exit 0
fi
cleanup() { rm -rf "$LOCK_DIR" 2>/dev/null || true; }
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
