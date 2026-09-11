#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AbleSci.com 每日自动签到 —— 多账号版

设计要点（都是踩过的坑）：
  1. CSRF token 必须【每次请求重新抓取】。站点会轮换 token，且每张登录页
     只对应当次会话；用旧的或空的 token 提交会被要求输入验证码。
  2. 页面标记已改版：token 在 <input name="_csrf"> 和 <meta name="csrf-token">，
     旧的 id="csrf-val" 已失效（老版本脚本正是卡在这里直接 sys.exit(1)）。
  3. 仓库可能是公开的，Actions 日志同样公开。所有邮箱一律打码输出。

支持的环境变量：
  ABLESCI_1_EMAIL / ABLESCI_1_PASSWORD     第 1 个账号
  ABLESCI_2_EMAIL / ABLESCI_2_PASSWORD     第 2 个账号（依此类推，1..20）
  ABLESCI_<n>_NAME                         可选，该账号的显示备注
  ABLESCI_EMAIL   / ABLESCI_PASSWORD       旧的单账号写法，仍然兼容

退出码：全部成功（含"今日已签"）为 0，任一账号失败为 1，
        便于 GitHub Actions 变红报警。
"""

from __future__ import annotations

import html
import os
import random
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import requests

BASE = "https://www.ablesci.com"
LOGIN_URL = f"{BASE}/site/login"
SIGN_URL = f"{BASE}/user/sign"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 让请求头尽可能贴近真实浏览器。不是决定性的（数据中心 IP 才是最大特征），
# 但能减少一些廉价的风控特征命中。注意刻意不覆盖 Accept-Encoding，
# 交给 requests 依据已安装的解码库自行决定，否则可能收到无法解压的响应。
DOC_HEADERS = {
    "User-Agent": UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="126", "Not:A-Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# 站点自己的 AJAX 请求头（登录、签到接口都走 XHR）
XHR_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Chromium";v="126", "Not:A-Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

MAX_ACCOUNT_SLOTS = 20
TIMEOUT = 25

# ---------------------------------------------------------------- CSRF 抓取
# 按优先级尝试；第一个命中的即采用。顺序刻意把新版标记放在最前面。
CSRF_PATTERNS = (
    re.compile(r'name="_csrf"\s+value="([^"]+)"'),
    re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"'),
    re.compile(r'name="csrf-token"\s+content="([^"]+)"'),
    re.compile(r'id="csrf-val"\s+value="([^"]+)"'),  # 旧版标记，保底
)

# ------------------------------------------------------- 积分/连签天数解析
POINT_PATTERNS = (
    re.compile(r"当前拥有<[^>]*>\s*(\d+)\s*</cite>\s*积分"),
    re.compile(r"当前拥有[^0-9]{0,80}?(\d+)[^0-9]{0,20}?积分"),
)
STREAK_PATTERNS = (
    re.compile(r"已连续签到<[^>]*>\s*(\d+)\s*</cite>"),
    re.compile(r"已连续签到[^0-9]{0,80}?(\d+)"),
)

# 结果状态
OK = "ok"            # 签到成功
ALREADY = "already"  # 今天已经签过
FAILED = "failed"    # 站点明确拒绝（密码错等）
CAPTCHA = "captcha"  # 被要求输入验证码
ERROR = "error"      # 网络/解析等异常


# ------------------------------------------------------------------- 工具
def read_env(name: str, *, strip_whitespace: bool) -> str:
    """读取环境变量并清理污染字符。

    踩过的坑：在 Windows PowerShell 5.1 下用
        echo "value" | gh secret set NAME
    写入 Secret，值会被混入 UTF-8 BOM(\\uFEFF) 和尾随 CRLF。存进仓库后
    完全看不出来（Secret 不可读、掩码后只差一个不可见字符），但登录会一直
    报「邮箱或密码错误」，极难排查。

    这里统一清理：
      - BOM 一律去掉；
      - 邮箱额外去掉首尾空白；
      - 密码只去掉首尾 CR/LF，避免误伤本身以空格开头或结尾的密码。
    """
    raw = os.environ.get(name) or ""
    cleaned = raw.replace("\ufeff", "").strip("\r\n")
    return cleaned.strip() if strip_whitespace else cleaned


def mask_email(email: str) -> str:
    """打码邮箱，避免公开仓库的 Actions 日志泄露账号。"""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    head = local[0] if len(local) <= 2 else local[:2]
    return f"{head}***@{domain}"


def find_first(patterns, text):
    for pat in patterns:
        m = pat.search(text)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def parse_json(resp):
    """站点异常时可能返回 HTML（WAF 拦截页），这里统一兜底。"""
    try:
        return resp.json()
    except ValueError:
        snippet = resp.text[:200].replace("\n", " ")
        return {"code": -1, "msg": f"非 JSON 响应 (HTTP {resp.status_code}): {snippet}"}


def clean_already_message(msg: str) -> str:
    """站点原文类似「签到失败，您今天已于 [13:45:52] 签到。」

    这是"今天已经签过"的正常情况，但自带「失败」二字容易被误读成出错，
    这里把前缀去掉，只保留有用信息（含签到时间）。
    """
    cleaned = re.sub(r"^签到失败[，,、:：]?\s*", "", msg or "").strip()
    return cleaned or "今天已经签到过了"


@dataclass
class Account:
    label: str
    email: str
    password: str

    @property
    def display(self) -> str:
        return f"{self.label} ({mask_email(self.email)})"


@dataclass
class Outcome:
    account: Account
    status: str
    message: str
    points: str | None = None
    streak: str | None = None


# ------------------------------------------------------------ 账号收集
def collect_accounts() -> list[Account]:
    accounts: list[Account] = []
    seen: set[str] = set()

    for i in range(1, MAX_ACCOUNT_SLOTS + 1):
        email = read_env(f"ABLESCI_{i}_EMAIL", strip_whitespace=True)
        password = read_env(f"ABLESCI_{i}_PASSWORD", strip_whitespace=False)
        name = read_env(f"ABLESCI_{i}_NAME", strip_whitespace=True)
        if not email and not password:
            continue
        if not email or not password:
            print(f"⚠️  ABLESCI_{i}_* 配置不完整（邮箱和密码必须同时提供），已跳过")
            continue
        if email in seen:
            continue
        seen.add(email)
        accounts.append(Account(name or f"账号{i}", email, password))

    # 旧的单账号写法，保持向后兼容
    email = read_env("ABLESCI_EMAIL", strip_whitespace=True)
    password = read_env("ABLESCI_PASSWORD", strip_whitespace=False)
    if email and password and email not in seen:
        accounts.append(Account("默认账号", email, password))

    return accounts


# ------------------------------------------------------------ 签到客户端
class AbleSciClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(DOC_HEADERS)

    def fresh_csrf(self) -> str:
        """每次都重新 GET 登录页并抓取当次有效的 token。"""
        resp = self.session.get(
            LOGIN_URL, headers={"Referer": BASE + "/"}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        token = find_first(CSRF_PATTERNS, resp.text)
        if not token:
            raise RuntimeError(
                "登录页未找到 CSRF token —— 站点可能再次改版，"
                "请更新 ablesci_signin.py 里的 CSRF_PATTERNS"
            )
        return token

    def login(self, email: str, password: str) -> dict:
        csrf = self.fresh_csrf()
        headers = dict(XHR_HEADERS)
        headers["Referer"] = LOGIN_URL
        headers["Origin"] = BASE
        resp = self.session.post(
            LOGIN_URL,
            data={"_csrf": csrf, "email": email, "password": password},
            headers=headers,
            timeout=TIMEOUT,
        )
        return parse_json(resp)

    def sync(self) -> None:
        """登录后访问首页，让服务端会话状态落定。"""
        self.session.get(BASE + "/", headers={"Referer": LOGIN_URL}, timeout=TIMEOUT)

    def sign(self) -> dict:
        headers = dict(XHR_HEADERS)
        headers["Referer"] = BASE + "/"
        resp = self.session.get(SIGN_URL, headers=headers, timeout=TIMEOUT)
        return parse_json(resp)

    def stats(self) -> tuple[str | None, str | None]:
        """读取积分与连续签到天数；解析不到不算失败。"""
        try:
            resp = self.session.get(BASE + "/", timeout=TIMEOUT)
        except requests.RequestException:
            return None, None
        return (
            find_first(POINT_PATTERNS, resp.text),
            find_first(STREAK_PATTERNS, resp.text),
        )


# ---------------------------------------------------------------- 单账号流程
def run_account(account: Account, max_attempts: int = 2) -> Outcome:
    last: Outcome | None = None

    for attempt in range(1, max_attempts + 1):
        client = AbleSciClient()  # 每次都全新会话，避免脏 cookie
        try:
            result = client.login(account.email, account.password)
        except requests.RequestException as exc:
            last = Outcome(account, ERROR, f"网络异常：{exc}")
            continue
        except RuntimeError as exc:
            # CSRF 抓不到属于结构性问题，重试也没用
            return Outcome(account, ERROR, str(exc))

        code = result.get("code")
        msg = str(result.get("msg") or "").strip()

        if code != 0:
            if "验证码" in msg:
                last = Outcome(
                    account,
                    CAPTCHA,
                    "站点要求输入验证码（通常是密码错误多次或 token 失效导致）。"
                    "请先在浏览器登录一次并完成验证码，再重跑本任务。",
                )
                if attempt < max_attempts:
                    time.sleep(3)
                    continue
                return last
            # 账号密码本身不对，重试无意义
            return Outcome(account, FAILED, f"登录失败：{msg or '未知错误'}")

        # 登录成功
        try:
            client.sync()
            sign_result = client.sign()
        except requests.RequestException as exc:
            return Outcome(account, ERROR, f"签到请求异常：{exc}")

        sign_code = sign_result.get("code")
        sign_msg = str(sign_result.get("msg") or "").strip()
        points, streak = client.stats()

        if sign_code == 0:
            return Outcome(account, OK, sign_msg or "签到成功", points, streak)

        if sign_code == 1:
            if "需要登录" in sign_msg:
                last = Outcome(account, ERROR, f"登录态未保持：{sign_msg}")
                continue
            if "已" in sign_msg:
                return Outcome(
                    account, ALREADY, clean_already_message(sign_msg), points, streak
                )
            return Outcome(
                account, FAILED, f"签到失败：{sign_msg or '未知错误'}", points, streak
            )

        return Outcome(account, FAILED, f"签到返回异常：{sign_result}", points, streak)

    return last or Outcome(account, ERROR, "未知错误")


# ------------------------------------------------------------------ 输出
STATUS_ICON = {
    OK: "✅",
    ALREADY: "ℹ️",
    FAILED: "❌",
    CAPTCHA: "🤖",
    ERROR: "⚠️",
}
STATUS_TEXT = {
    OK: "签到成功",
    ALREADY: "今日已签到",
    FAILED: "失败",
    CAPTCHA: "被要求验证码",
    ERROR: "异常",
}


def print_report(outcomes: list[Outcome]) -> None:
    print("\n" + "=" * 62)
    print("AbleSci 签到结果汇总")
    print("=" * 62)
    for o in outcomes:
        icon = STATUS_ICON.get(o.status, "?")
        print(f"\n{icon} {o.account.display} —— {STATUS_TEXT.get(o.status, o.status)}")
        print(f"   说明：{o.message}")
        if o.points is not None:
            print(f"   当前积分：{o.points}")
        if o.streak is not None:
            print(f"   连续签到：{o.streak} 天")
    print("\n" + "=" * 62)


def write_step_summary(outcomes: list[Outcome]) -> None:
    """写到 GitHub Actions 的 Job Summary，在网页上直接可看。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## AbleSci 每日签到结果\n\n")
            fh.write("| 账号 | 状态 | 说明 | 积分 | 连签 |\n")
            fh.write("| --- | --- | --- | --- | --- |\n")
            for o in outcomes:
                icon = STATUS_ICON.get(o.status, "?")
                msg = o.message.replace("|", "\\|").replace("\n", " ")
                fh.write(
                    f"| {o.account.display} | {icon} {STATUS_TEXT.get(o.status, o.status)} "
                    f"| {msg} | {o.points or '-'} | {o.streak or '-'} |\n"
                )
    except OSError as exc:
        print(f"（写入 Step Summary 失败：{exc}）")


def check_site() -> int:
    """--check：不登录，只验证站点可达且 CSRF 仍能抓到。用于排查改版。"""
    print("自检：正在验证站点可达性与 CSRF 提取逻辑……")
    try:
        token = AbleSciClient().fresh_csrf()
    except Exception as exc:  # noqa: BLE001 - 自检就是要兜住所有异常
        print(f"❌ 自检失败：{exc}")
        return 1
    print(f"✅ 站点可达，CSRF token 抓取成功（前缀 {token[:10]}…，长度 {len(token)}）")
    return 0


def apply_jitter() -> None:
    """定时触发时随机延迟，打散每天几乎相同的签到时刻。

    实测：原 cron(00:00 UTC) 的真实执行时刻会成段稳定在同一偏移上，
    例如连续 12 天落在 01:35~01:45 之间，相差不超过 10 分钟。
    这种规律性本身是个可被识别的特征，所以在 GitHub 自身漂移之上
    再叠加一层随机。

    只对 schedule 触发生效，手动 Run workflow 不会白白空等。
    窗口由 ABLESCI_JITTER_MAX 控制（秒），未设置或为 0 则关闭。
    """
    raw = read_env("ABLESCI_JITTER_MAX", strip_whitespace=True)
    try:
        jitter_max = int(raw or 0)
    except ValueError:
        print(f"⚠️  ABLESCI_JITTER_MAX 不是整数（{raw!r}），已忽略")
        return
    if jitter_max <= 0:
        return

    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        print("（非定时触发，跳过随机延迟，以免手动运行空等）")
        return

    wait = random.randint(0, jitter_max)
    print(
        f"⏱  随机延迟 {wait // 60} 分 {wait % 60} 秒后开始签到"
        f"（窗口 0~{jitter_max // 60} 分钟，用于打散每日签到时刻）"
    )
    time.sleep(wait)


# ------------------------------------------------------------ 邮件通知
# 用标准库 smtplib 发信，不引入任何额外依赖。
#
# ⚠️ 关键坑：GitHub Actions 跑在 Azure 上，Azure 出于 IP 信誉考虑封锁了
# 出站 25 端口，因此必须使用 465(SSL) 或 587(STARTTLS)。
# 「本地用 25 端口发得出去，换到 Actions 就失败」基本都是这个原因。

MAIL_WHEN_VALUES = ("always", "failure", "never")


def mail_config() -> dict | None:
    """读取邮件配置。三个必填项不齐时返回 None（视为未启用）。"""
    host = read_env("SMTP_HOST", strip_whitespace=True)
    user = read_env("SMTP_USER", strip_whitespace=True)
    password = read_env("SMTP_PASS", strip_whitespace=False)

    missing = [
        name
        for name, value in (("SMTP_HOST", host), ("SMTP_USER", user), ("SMTP_PASS", password))
        if not value
    ]
    if missing:
        if len(missing) < 3:  # 配了一部分才算"配置错误"，全空是正常的
            print(f"⚠️  邮件配置不完整，缺少 {'、'.join(missing)}，已跳过邮件通知")
        return None

    port_raw = read_env("SMTP_PORT", strip_whitespace=True)
    try:
        port = int(port_raw) if port_raw else 465
    except ValueError:
        print(f"⚠️  SMTP_PORT 不是整数（{port_raw!r}），已回退到 465")
        port = 465

    to_raw = read_env("MAIL_TO", strip_whitespace=True)
    recipients = [a.strip() for a in to_raw.replace(";", ",").split(",") if a.strip()]
    if not recipients:
        recipients = [user]

    when = (read_env("MAIL_WHEN", strip_whitespace=True) or "always").lower()
    if when not in MAIL_WHEN_VALUES:
        print(f"⚠️  MAIL_WHEN 取值无效（{when!r}），已回退到 always")
        when = "always"

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "recipients": recipients,
        "when": when,
    }


def build_subject(outcomes: list[Outcome]) -> str:
    total = len(outcomes)
    ok = sum(1 for o in outcomes if o.status in (OK, ALREADY))
    stamp = time.strftime("%m-%d %H:%M")
    if ok == total:
        return f"✅ AbleSci 签到成功 {ok}/{total}（{stamp}）"
    return f"❌ AbleSci 签到异常 {total - ok}/{total}（{stamp}）"


def build_bodies(outcomes: list[Outcome]) -> tuple[str, str]:
    """返回 (纯文本, HTML) 两份正文。"""
    lines = ["AbleSci 每日签到结果", "=" * 34, ""]
    for o in outcomes:
        lines.append(
            f"{STATUS_ICON.get(o.status, '?')} {o.account.display}"
            f" —— {STATUS_TEXT.get(o.status, o.status)}"
        )
        lines.append(f"   {o.message}")
        if o.points is not None:
            lines.append(f"   当前积分：{o.points}")
        if o.streak is not None:
            lines.append(f"   连续签到：{o.streak} 天")
        lines.append("")
    text_body = "\n".join(lines)

    cells = []
    for o in outcomes:
        cells.append(
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #eee">{html.escape(o.account.display)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;white-space:nowrap">'
            f'{STATUS_ICON.get(o.status, "?")} {html.escape(STATUS_TEXT.get(o.status, o.status))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{html.escape(o.message)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:right">{html.escape(o.points or "-")}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:right">{html.escape(o.streak or "-")}</td>'
            "</tr>"
        )
    html_body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,'
        '\'Helvetica Neue\',Arial,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;color:#222">'
        f'<h2 style="margin:0 0 4px">{html.escape(build_subject(outcomes))}</h2>'
        '<p style="color:#888;margin:0 0 16px;font-size:13px">由 GitHub Actions 自动发送</p>'
        '<table style="border-collapse:collapse;font-size:14px">'
        '<thead><tr style="background:#f6f8fa">'
        '<th style="padding:8px;text-align:left">账号</th>'
        '<th style="padding:8px;text-align:left">状态</th>'
        '<th style="padding:8px;text-align:left">说明</th>'
        '<th style="padding:8px;text-align:right">积分</th>'
        '<th style="padding:8px;text-align:right">连签</th>'
        "</tr></thead><tbody>"
        + "".join(cells)
        + "</tbody></table></div>"
    )
    return text_body, html_body


def send_mail(outcomes: list[Outcome]) -> None:
    """发送签到结果邮件。任何失败都只告警，不影响签到本身的退出码。"""
    cfg = mail_config()
    if cfg is None:
        return

    if cfg["when"] == "never":
        print("（MAIL_WHEN=never，已跳过邮件通知）")
        return

    failed = [o for o in outcomes if o.status in (FAILED, CAPTCHA, ERROR)]
    if cfg["when"] == "failure" and not failed:
        print("（MAIL_WHEN=failure 且本次全部成功，已跳过邮件通知）")
        return

    subject = build_subject(outcomes)
    text_body, html_body = build_bodies(outcomes)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg["user"]
    message["To"] = ", ".join(cfg["recipients"])
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as server:
                server.login(cfg["user"], cfg["password"])
                server.send_message(message)
        else:
            # 587 走 STARTTLS；注意 25 端口在 Azure 上是被封锁的
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg["user"], cfg["password"])
                server.send_message(message)
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不能让签到任务变红
        print(f"⚠️  邮件发送失败（不影响签到结果）：{type(exc).__name__}: {exc}")
        return

    shown = "、".join(mask_email(r) for r in cfg["recipients"])
    print(f"📧 邮件已发送至 {shown}")


# ------------------------------------------------------------------ main
def main() -> int:
    if "--check" in sys.argv:
        return check_site()

    accounts = collect_accounts()
    if not accounts:
        print("❌ 没有发现任何账号配置。")
        print("   请在仓库 Settings → Secrets and variables → Actions 里添加：")
        print("     ABLESCI_1_EMAIL / ABLESCI_1_PASSWORD")
        return 1

    print(f"发现 {len(accounts)} 个账号：")
    for acc in accounts:
        print(f"  - {acc.display}")

    apply_jitter()

    outcomes: list[Outcome] = []
    for idx, account in enumerate(accounts, 1):
        print(f"\n[{idx}/{len(accounts)}] 处理 {account.display} ……")
        outcome = run_account(account)
        print(f"   → {STATUS_ICON.get(outcome.status, '?')} {outcome.message}")
        outcomes.append(outcome)
        if idx < len(accounts):
            # 账号之间随机间隔，比固定值更像真人、也更温和
            gap = random.uniform(4, 15)
            print(f"   （等待 {gap:.1f} 秒后处理下一个账号）")
            time.sleep(gap)

    print_report(outcomes)
    write_step_summary(outcomes)
    send_mail(outcomes)

    bad = [o for o in outcomes if o.status in (FAILED, CAPTCHA, ERROR)]
    if bad:
        print(f"\n❌ {len(bad)} 个账号未成功，退出码 1")
        return 1
    print("\n✅ 全部账号处理完毕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
