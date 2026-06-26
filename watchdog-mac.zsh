#!/bin/zsh
set -u

# 库存监控看门狗：每隔几分钟检查主脚本最后一次“成功心跳”的时间。
# 超过阈值仍无心跳 → 判定监控已停止 → 尝试自动重启 + Telegram 提醒；恢复后再发一条“已恢复”。
MONITOR_DIR="/Users/bill/Library/Application Support/giftcard-monitor"
HEARTBEAT="$MONITOR_DIR/.heartbeat"
STATE="$MONITOR_DIR/.watchdog-state"        # 内容：up / down，用于去重和发“恢复”通知
LOG="$MONITOR_DIR/watchdog.log"
ENV_FILE="$MONITOR_DIR/.env"
LABEL="com.bill.giftcard-monitor"
THRESHOLD=900     # 秒：超过 15 分钟（约 3 个运行周期）没有成功心跳就判定为停止
AUTO_RESTART=1    # 1=检测到停止时先尝试 kickstart 重启主任务

timestamp() { date "+%Y-%m-%d %H:%M:%S %Z"; }
log() { echo "$(timestamp) $1" >> "$LOG"; }

tg_send() {
  local text="$1" token chat
  token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')
  chat=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')
  if [ -z "$token" ] || [ -z "$chat" ]; then
    log "Telegram 未配置，跳过推送"
    return 1
  fi
  curl -s -m 15 -o /dev/null \
    --data-urlencode "chat_id=$chat" \
    --data-urlencode "text=$text" \
    "https://api.telegram.org/bot${token}/sendMessage" \
    && log "Telegram 已推送" || log "Telegram 推送失败"
}

now=$(date +%s)

# 心跳文件不存在（刚装好、还没成功跑过）→ 初始化为现在，不误报
if [ ! -f "$HEARTBEAT" ]; then
  echo "$now" > "$HEARTBEAT"
  log "心跳文件缺失，已初始化为当前时间"
  echo "up" > "$STATE"
  exit 0
fi

last=$(cat "$HEARTBEAT" 2>/dev/null)
[[ "$last" =~ ^[0-9]+$ ]] || last=0
age=$(( now - last ))

prev="up"
[ -f "$STATE" ] && prev=$(cat "$STATE" 2>/dev/null)

if [ "$age" -gt "$THRESHOLD" ]; then
  # —— 判定为停止 ——
  if [ "$prev" != "down" ]; then
    mins=$(( age / 60 ))
    log "检测到停止：已 ${age}s（约 ${mins} 分钟）无成功心跳，阈值 ${THRESHOLD}s"
    # 夜间休眠常把跑到一半的进程杀掉、清理 trap 来不及执行 → 残留“僵死锁”，
    # 会让重启后的新进程继续空转。这里先把“持有者已死”的锁清掉，确保唤醒后能真正恢复。
    LOCK_DIR="$MONITOR_DIR/.mac-launchd.lock"
    if [ -d "$LOCK_DIR" ]; then
      lpid=$(cat "$LOCK_DIR/pid" 2>/dev/null)
      if [ -z "$lpid" ] || ! kill -0 "$lpid" 2>/dev/null; then
        rm -rf "$LOCK_DIR" && log "已清除僵死锁（pid=${lpid:-none}），便于重启后立即恢复"
      fi
    fi
    action="未自动重启"
    if [ "$AUTO_RESTART" -eq 1 ]; then
      if launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>>"$LOG"; then
        action="已尝试自动重启"
        log "已执行 kickstart 重启 $LABEL"
      else
        action="自动重启失败（请手动检查）"
        log "kickstart 失败"
      fi
    fi
    tg_send "⚠️ 库存监控疑似停止
已约 ${mins} 分钟没有成功运行。
处理：${action}
时间：$(timestamp)"
    echo "down" > "$STATE"
  fi
else
  # —— 健康 ——
  if [ "$prev" = "down" ]; then
    log "已恢复：${age}s 前有成功心跳"
    tg_send "✅ 库存监控已恢复正常运行。
时间：$(timestamp)"
  fi
  echo "up" > "$STATE"
fi
exit 0
