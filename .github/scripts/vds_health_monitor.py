#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


STATE_PATH = Path(".github/vds-health-state.json")

ISSUE_NAMES = {
    "external_tcp_23845_unreachable": "外部无法连接代理端口 23845/TCP",
    "ssh_health_check_failed": "无法通过受限 SSH 获取健康状态",
    "xray_active": "Xray 服务未运行",
    "tcp_23845_listening": "23845/TCP 未监听",
    "udp_23845_listening": "23845/UDP 未监听",
    "ufw_23845_udp_allowed": "防火墙未开放 23845/UDP",
    "direct_https_ok": "VDS 无法直接访问外网",
    "proxy_end_to_end_ok": "代理端到端测试失败",
    "disk_usage_high": "磁盘使用率过高",
    "memory_available_low": "可用内存过低",
    "swap_missing": "Swap 未启用",
    "swap_usage_high": "Swap 使用率过高",
    "load_high": "系统负载过高",
    "conntrack_usage_high": "连接跟踪表接近满载",
}


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def tcp_reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=8):
            return True
    except OSError:
        return False


def remote_health(host, ssh_port, key_path, known_hosts_path):
    command = [
        "ssh",
        "-i",
        key_path,
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-p",
        str(ssh_port),
        f"root@{host}",
        "health-check",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=35,
            check=False,
        )
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        if not lines:
            return None
        return json.loads(lines[-1])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def check_once():
    host = required("VDS_HOST")
    ssh_port = int(required("VDS_SSH_PORT"))
    proxy_port = int(os.environ.get("VDS_PROXY_PORT", "23845"))
    tcp_ok = tcp_reachable(host, proxy_port)
    health = remote_health(
        host,
        ssh_port,
        required("VDS_KEY_PATH"),
        required("VDS_KNOWN_HOSTS_PATH"),
    )
    if health is None:
        return {
            "status": "critical",
            "boot_id": "",
            "issues": ["ssh_health_check_failed"]
            + ([] if tcp_ok else ["external_tcp_23845_unreachable"]),
            "warnings": [],
            "metrics": {},
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    if not tcp_ok:
        health["status"] = "critical"
        health.setdefault("issues", []).append("external_tcp_23845_unreachable")
    return health


def stable_check():
    result = check_once()
    if result["status"] == "ok":
        return result
    time.sleep(20)
    return check_once()


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "boot_id": "", "updated_at": ""}


def format_metrics(metrics):
    if not metrics:
        return "暂无资源数据"
    return (
        f"负载 {metrics.get('load1', '?')}，"
        f"可用内存 {metrics.get('mem_available_pct', '?')}%，"
        f"Swap 已用 {metrics.get('swap_used_pct', '?')}%，"
        f"磁盘已用 {metrics.get('disk_used_pct', '?')}%，"
        f"连接表 {metrics.get('conntrack_pct', '?')}%"
    )


def describe(items):
    return "；".join(ISSUE_NAMES.get(item, item) for item in items) or "无"


def build_message(previous, current, force):
    status = current["status"]
    boot_changed = bool(
        previous.get("boot_id")
        and current.get("boot_id")
        and previous["boot_id"] != current["boot_id"]
    )
    if not force and status == previous.get("status") and not boot_changed:
        return ""
    if status == "ok" and previous.get("status") in {"critical", "warning"}:
        title = "[VDS恢复] 服务已经恢复正常"
    elif boot_changed:
        title = "[VDS重启] 检测到服务器启动记录变化"
    elif status == "critical":
        title = "[VDS告警] 服务异常"
    elif status == "warning":
        title = "[VDS预警] 资源状态需要关注"
    else:
        title = "[VDS监控] 监控已启用，当前正常"
    details = current.get("issues", []) + current.get("warnings", [])
    return "\n".join(
        [
            title,
            f"状态：{status}",
            f"情况：{describe(details)}",
            f"资源：{format_metrics(current.get('metrics', {}))}",
            f"检查时间：{current.get('timestamp', '')}",
        ]
    )


def send_telegram(message):
    payload = json.dumps(
        {"chat_id": required("TELEGRAM_CHAT_ID"), "text": message},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{required('TELEGRAM_BOT_TOKEN')}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError):
        return False
    return bool(result.get("ok"))


def main():
    previous = load_state()
    current = stable_check()
    force = os.environ.get("FORCE_NOTIFY", "0") == "1"
    message = build_message(previous, current, force)
    if not message:
        print(f"VDS status unchanged: {current['status']}")
        return 0
    if not send_telegram(message):
        print("Telegram notification failed; state was not advanced")
        return 1
    STATE_PATH.write_text(
        json.dumps(
            {
                "status": current["status"],
                "boot_id": current.get("boot_id", ""),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Telegram notification sent for VDS state: {current['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
