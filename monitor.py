#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""库存监控（云端每 5 分钟一轮）。目标地址/类目/标签均由环境变量注入，代码不含可识别信息。

站点1：商品 JSON API —— 任一规格有货即提醒。
站点2：页面内嵌商品 JSON —— 面额 >= 阈值 的单品『有货』即提醒（忽略套餐 bundle）。
站点3：服务端渲染页面的单选项 —— 面额 >= 阈值 的选项『有货』（无 disabled）即提醒。
站点4：列表页 —— 页面上『出现』面额 >= 阈值 即提醒（按面额去重：消失后再出现会再报）。
各站均按『售罄→有货』去重：有货才报一次，持续有货不重复；无命中静默不发。
某站点抓取失败则跳过且不改写其状态（不误报）。

环境变量：
  邮件：SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD EMAIL_FROM EMAIL_TO [SMTP_USE_SSL]
  目标：S1_API S1_PAGE S2_PAGE S2_CATEGORY [S2_MIN_AMOUNT=10000] S3_PAGE [S3_MIN_AMOUNT=10000]
        S4_PAGE [S4_MIN_AMOUNT=10000]
  标签：[S1_LABEL=站点1] [S2_LABEL=站点2] [S3_LABEL=站点3] [S4_LABEL=站点4]
  调试：FORCE_SEND=1（无命中也发一封测试邮件）
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

# 本地运行时尝试加载 .env（云端无此文件也不报错）
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 25

S1_API = os.getenv("S1_API", "")
S1_PAGE = os.getenv("S1_PAGE", "")
S2_PAGE = os.getenv("S2_PAGE", "")
S2_CATEGORY = os.getenv("S2_CATEGORY", "")
S2_MIN_AMOUNT = int(os.getenv("S2_MIN_AMOUNT", "10000"))
S3_PAGE = os.getenv("S3_PAGE", "")
S3_MIN_AMOUNT = int(os.getenv("S3_MIN_AMOUNT", "10000"))
S4_PAGE = os.getenv("S4_PAGE", "")
S4_MIN_AMOUNT = int(os.getenv("S4_MIN_AMOUNT", "10000"))
S1_LABEL = os.getenv("S1_LABEL", "站点1")
S2_LABEL = os.getenv("S2_LABEL", "站点2")
S3_LABEL = os.getenv("S3_LABEL", "站点3")
S4_LABEL = os.getenv("S4_LABEL", "站点4")

AMOUNT_RE = re.compile(r"(\d[\d,]*)\s*NGN", re.I)
# 站点3：单选项 input + 对应 label（label 文本形如 "10000 ngn"；input 含 disabled 即售罄）
S3_INPUT_RE = re.compile(r'<input[^>]*?id="CheckedOption_(\d+)"[^>]*>', re.I)
S3_LABEL_RE = re.compile(r'<label\s+for="CheckedOption_(\d+)"[^>]*>\s*(\d[\d\s,]*)\s*NGN', re.I)

logger = logging.getLogger("monitor")


# ---------- 状态 ----------
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 state.json 失败，按空状态处理：%s", e)
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- 站点 1（JSON API：任一规格有货）----------
def check_s1(prev_in_stock: list[str]) -> tuple[list[dict], list[str]]:
    """返回 (新有货告警, 当前有货 sku_code 列表)。抓取/解析失败抛异常。"""
    r = requests.get(S1_API, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if body.get("status_code") != 0:
        raise RuntimeError(f"S1 status_code={body.get('status_code')} msg={body.get('msg')}")
    skus = body["data"]["skus"]
    now: list[str] = []
    detail: dict[str, dict] = {}
    for s in skus:
        if not s.get("is_active"):
            continue
        auto = int(s.get("auto_stock_available") or 0)
        up = int(s.get("upstream_stock") or 0)
        manual = int(s.get("manual_stock_total") or 0) - int(s.get("manual_stock_sold") or 0)
        if auto > 0 or up > 0 or manual > 0:
            code = s.get("sku_code")
            now.append(code)
            sv = s.get("spec_values") or {}
            detail[code] = {
                "spec": sv.get("zh-CN") or sv.get("en-US") or code,
                "price": s.get("price_amount"),
                "qty": max(auto, up, manual),
            }
    prev = set(prev_in_stock or [])
    return [detail[c] for c in now if c not in prev], now


# ---------- 站点 2（页面内嵌 JSON：>=阈值 单品有货）----------
def _parse_category_objects(html: str, category: str) -> list[dict]:
    """花括号配对扫描，稳健提取含指定类目的扁平商品对象（套餐含嵌套花括号会自动跳过）。"""
    if not category:
        return []
    key = '"card_category_code":"' + category + '"'
    out: list[dict] = []
    seen: set[str] = set()
    i = 0
    while True:
        p = html.find(key, i)
        if p < 0:
            break
        i = p + len(key)
        left = html.rfind("{", 0, p)
        right = html.find("}", p)
        if left < 0 or right < 0:
            continue
        chunk = html[left : right + 1]
        if chunk in seen:
            continue
        seen.add(chunk)
        try:
            out.append(json.loads(chunk))
        except Exception:  # noqa: BLE001 —— 套餐等含嵌套花括号者解析失败即跳过
            continue
    return out


def check_s2(prev_in_stock: list[int]) -> tuple[list[dict], list[int]]:
    """返回 (新有货的 >=阈值 单品告警, 当前有货的 >=阈值 单品 id 列表)。抓取/解析失败抛异常。"""
    r = requests.get(S2_PAGE, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    objs = _parse_category_objects(r.text, S2_CATEGORY)
    if not objs:
        raise RuntimeError("站点2未解析到任何商品对象（页面结构可能已变化）")
    instock: dict[int, dict] = {}
    for d in objs:
        name = (d.get("name_us") or d.get("name") or "").strip()
        if "bundle" in name.lower():
            continue  # 套餐不算单品
        m = AMOUNT_RE.search(name)
        if not m:
            continue
        amount = int(m.group(1).replace(",", ""))
        if amount < S2_MIN_AMOUNT:
            continue
        count = int(d.get("card_count") or 0)
        if count > 0:
            instock[int(d["id"])] = {"name": name, "amount": amount, "count": count}
    prev = set(prev_in_stock or [])
    new_alerts = [{"id": i, **v} for i, v in instock.items() if i not in prev]
    return new_alerts, sorted(instock.keys())


# ---------- 站点 3（服务端渲染单选项：>=阈值 有货）----------
def check_s3(prev_in_stock: list[int]) -> tuple[list[dict], list[int]]:
    """返回 (新有货的 >=阈值 选项告警, 当前有货的 >=阈值 选项 id 列表)。抓取/解析失败抛异常。"""
    r = requests.get(S3_PAGE, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text
    disabled: dict[int, bool] = {}
    for m in S3_INPUT_RE.finditer(html):
        disabled[int(m.group(1))] = "disabled" in m.group(0).lower()
    amounts: dict[int, int] = {
        int(m.group(1)): int(re.sub(r"[\s,]", "", m.group(2)))
        for m in S3_LABEL_RE.finditer(html)
    }
    if not amounts:
        raise RuntimeError("站点3未解析到任何面额选项（页面结构可能已变化）")
    instock: dict[int, dict] = {}
    for oid, amount in amounts.items():
        if amount < S3_MIN_AMOUNT:
            continue
        if disabled.get(oid, True):  # 找不到对应 input 时按售罄处理，避免误报
            continue
        instock[oid] = {"name": f"{amount} NGN", "amount": amount}
    prev = set(prev_in_stock or [])
    new_alerts = [{"id": i, **v} for i, v in instock.items() if i not in prev]
    return new_alerts, sorted(instock.keys())


# ---------- 站点 4（列表页：出现 >=阈值 面额即报）----------
def check_s4(prev_present: list[int]) -> tuple[list[dict], list[int]]:
    """返回 (新出现的 >=阈值 面额告警, 当前页面上 >=阈值 的面额列表)。抓取/解析失败抛异常。"""
    r = requests.get(S4_PAGE, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    amounts = {int(m.group(1).replace(",", "")) for m in AMOUNT_RE.finditer(r.text)}
    if not amounts:
        # 正常情况下列表页至少有低面额商品；一个都解析不到多半是被拦截/改版
        raise RuntimeError("站点4未解析到任何面额（页面结构可能已变化或被拦截）")
    present = sorted(a for a in amounts if a >= S4_MIN_AMOUNT)
    prev = set(prev_present or [])
    new_alerts = [{"name": f"{a} NGN", "amount": a} for a in present if a not in prev]
    return new_alerts, present


# ---------- 邮件 ----------
def _smtp_ready() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("EMAIL_TO"))


def send_email(subject: str, html_body: str, text_body: str) -> bool:
    if not _smtp_ready():
        logger.error("未配置 SMTP / 收件人，无法发送邮件")
        return False
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD", "")
    email_from = os.getenv("EMAIL_FROM") or user
    email_to = [x.strip() for x in os.getenv("EMAIL_TO", "").split(",") if x.strip()]
    use_ssl = os.getenv("SMTP_USE_SSL", "true").strip().lower() in {"1", "true", "yes", "on"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("库存监控", email_from))
    msg["To"] = ", ".join(email_to)
    msg.attach(MIMEText(text_body or "请使用支持 HTML 的客户端查看。", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    server = None
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.starttls()
        server.login(user, password)
        server.sendmail(email_from, email_to, msg.as_string())
        logger.info("邮件已发送给 %d 位收件人", len(email_to))
        return True
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


def build_email(
    s1_alerts: list[dict],
    s2_alerts: list[dict],
    s3_alerts: list[dict] | None = None,
    s4_alerts: list[dict] | None = None,
) -> tuple[str, str, str]:
    s3_alerts = s3_alerts or []
    s4_alerts = s4_alerts or []
    n1, n2, n3, n4 = len(s1_alerts), len(s2_alerts), len(s3_alerts), len(s4_alerts)
    subj_bits = []
    if n1:
        subj_bits.append(f"{S1_LABEL} {n1} 个规格有货")
    if n2:
        subj_bits.append(f"{S2_LABEL} {n2} 个≥{S2_MIN_AMOUNT}有货")
    if n3:
        subj_bits.append(f"{S3_LABEL} {n3} 个≥{S3_MIN_AMOUNT}有货")
    if n4:
        subj_bits.append(f"{S4_LABEL} 出现 {n4} 个≥{S4_MIN_AMOUNT}")
    subject = "【库存提醒】" + " / ".join(subj_bits)

    th, hh = [], []
    if n1:
        rows = "".join(
            f"<tr><td>{a['spec']}</td><td style='text-align:right'>{a['price']}</td>"
            f"<td style='text-align:right'>{a['qty']}</td></tr>"
            for a in s1_alerts
        )
        hh.append(
            f"<h3>■ {S1_LABEL}（有货）</h3>"
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<tr><th>规格</th><th>价格</th><th>可购数量</th></tr>"
            f"{rows}</table>"
            + (f"<p>下单页：<a href='{S1_PAGE}'>{S1_PAGE}</a></p>" if S1_PAGE else "")
        )
        th.append(f"■ {S1_LABEL}（有货）：")
        th += [f"  - {a['spec']}  价 {a['price']}  可购 {a['qty']}" for a in s1_alerts]
        if S1_PAGE:
            th.append(f"  下单页：{S1_PAGE}")
    if n2:
        rows = "".join(
            f"<tr><td>{a['name']}</td><td style='text-align:right'>{a['amount']} NGN</td>"
            f"<td style='text-align:right'>{a['count']}</td></tr>"
            for a in s2_alerts
        )
        hh.append(
            f"<h3>■ {S2_LABEL}（≥{S2_MIN_AMOUNT} NGN 单品有货）</h3>"
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<tr><th>名称</th><th>面额</th><th>库存</th></tr>"
            f"{rows}</table>"
            + (f"<p>页面：<a href='{S2_PAGE}'>{S2_PAGE}</a></p>" if S2_PAGE else "")
        )
        th.append(f"■ {S2_LABEL}（≥{S2_MIN_AMOUNT} NGN 单品有货）：")
        th += [f"  - {a['name']}  面额 {a['amount']} NGN  库存 {a['count']}" for a in s2_alerts]
        if S2_PAGE:
            th.append(f"  页面：{S2_PAGE}")
    if n3:
        rows = "".join(
            f"<tr><td>{a['name']}</td><td style='text-align:right'>{a['amount']} NGN</td></tr>"
            for a in s3_alerts
        )
        hh.append(
            f"<h3>■ {S3_LABEL}（≥{S3_MIN_AMOUNT} NGN 选项有货）</h3>"
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<tr><th>选项</th><th>面额</th></tr>"
            f"{rows}</table>"
            + (f"<p>下单页：<a href='{S3_PAGE}'>{S3_PAGE}</a></p>" if S3_PAGE else "")
        )
        th.append(f"■ {S3_LABEL}（≥{S3_MIN_AMOUNT} NGN 选项有货）：")
        th += [f"  - {a['name']}  面额 {a['amount']} NGN" for a in s3_alerts]
        if S3_PAGE:
            th.append(f"  下单页：{S3_PAGE}")
    if n4:
        rows = "".join(
            f"<tr><td style='text-align:right'>{a['amount']} NGN</td></tr>" for a in s4_alerts
        )
        hh.append(
            f"<h3>■ {S4_LABEL}（页面出现 ≥{S4_MIN_AMOUNT} NGN 面额）</h3>"
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            "<tr><th>面额</th></tr>"
            f"{rows}</table>"
            + (f"<p>列表页：<a href='{S4_PAGE}'>{S4_PAGE}</a></p>" if S4_PAGE else "")
        )
        th.append(f"■ {S4_LABEL}（页面出现 ≥{S4_MIN_AMOUNT} NGN 面额）：")
        th += [f"  - {a['amount']} NGN" for a in s4_alerts]
        if S4_PAGE:
            th.append(f"  列表页：{S4_PAGE}")

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    th.append(f"\n（检测时间 {now}）")
    text_body = "\n".join(th)
    html_body = (
        "<div style='font-family:sans-serif'>" + "".join(hh) + f"<p style='color:#888'>检测时间 {now}</p></div>"
    )
    return subject, html_body, text_body


# ---------- 主流程 ----------
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    state = load_state()

    s1_alerts: list[dict] = []
    s1_now = state.get("s1_in_stock", [])
    s1_ok = False
    try:
        s1_alerts, s1_now = check_s1(state.get("s1_in_stock", []))
        s1_ok = True
        logger.info("站点1：当前有货 %d 个，新有货 %d 个", len(s1_now), len(s1_alerts))
    except Exception as e:  # noqa: BLE001
        logger.error("站点1抓取失败，跳过：%s", e)

    s2_alerts: list[dict] = []
    s2_now = state.get("s2_in_stock", [])
    s2_ok = False
    try:
        s2_alerts, s2_now = check_s2(state.get("s2_in_stock", []))
        s2_ok = True
        logger.info("站点2：当前≥%d有货 %d 个，新有货 %d 个", S2_MIN_AMOUNT, len(s2_now), len(s2_alerts))
    except Exception as e:  # noqa: BLE001
        logger.error("站点2抓取失败，跳过：%s", e)

    s3_alerts: list[dict] = []
    s3_now = state.get("s3_in_stock", [])
    s3_ok = False
    if S3_PAGE:
        try:
            s3_alerts, s3_now = check_s3(state.get("s3_in_stock", []))
            s3_ok = True
            logger.info(
                "站点3：当前≥%d有货 %d 个，新有货 %d 个", S3_MIN_AMOUNT, len(s3_now), len(s3_alerts)
            )
        except Exception as e:  # noqa: BLE001
            logger.error("站点3抓取失败，跳过：%s", e)

    s4_alerts: list[dict] = []
    s4_now = state.get("s4_in_stock", [])
    s4_ok = False
    if S4_PAGE:
        try:
            s4_alerts, s4_now = check_s4(state.get("s4_in_stock", []))
            s4_ok = True
            logger.info(
                "站点4：当前≥%d出现 %d 个，新出现 %d 个", S4_MIN_AMOUNT, len(s4_now), len(s4_alerts)
            )
        except Exception as e:  # noqa: BLE001
            logger.error("站点4抓取失败，跳过：%s", e)

    if s1_alerts or s2_alerts or s3_alerts or s4_alerts:
        subject, html_body, text_body = build_email(s1_alerts, s2_alerts, s3_alerts, s4_alerts)
        send_email(subject, html_body, text_body)
    elif os.getenv("FORCE_SEND") == "1":
        logger.info("FORCE_SEND=1：发送一封测试邮件")
        send_email(
            "【库存监控】测试邮件",
            "<p>这是一封测试邮件，说明监控脚本与邮件通道工作正常。</p>",
            "这是一封测试邮件，说明监控脚本与邮件通道工作正常。",
        )
    else:
        logger.info("本轮无命中，不发邮件")

    if s1_ok:
        state["s1_in_stock"] = s1_now
    if s2_ok:
        state["s2_in_stock"] = s2_now
    if s3_ok:
        state["s3_in_stock"] = s3_now
    if s4_ok:
        state["s4_in_stock"] = s4_now
    # 清理旧键/易变字段，保持 state.json 稳定（存活性看 Actions 运行记录）
    for k in ("last_run_utc", "tz_in_stock", "seagm_seen_single_ids"):
        state.pop(k, None)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
