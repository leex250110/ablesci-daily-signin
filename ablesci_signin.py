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

import os
import re
import sys
import time
from dataclasses import dataclass

import requests

BASE = "https://www.ablesci.com"
LOGIN_URL = f"{BASE}/site/login"
SIGN_URL = f"{BASE}/user/sign"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

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
        email = (os.environ.get(f"ABLESCI_{i}_EMAIL") or "").strip()
        password = os.environ.get(f"ABLESCI_{i}_PASSWORD") or ""
        name = (os.environ.get(f"ABLESCI_{i}_NAME") or "").strip()
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
    email = (os.environ.get("ABLESCI_EMAIL") or "").strip()
    password = os.environ.get("ABLESCI_PASSWORD") or ""
    if email and password and email not in seen:
        accounts.append(Account("默认账号", email, password))

    return accounts


# ------------------------------------------------------------ 签到客户端
class AbleSciClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})

    def fresh_csrf(self) -> str:
        """每次都重新 GET 登录页并抓取当次有效的 token。"""
        resp = self.session.get(LOGIN_URL, timeout=TIMEOUT)
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
        resp = self.session.post(
            LOGIN_URL,
            data={"_csrf": csrf, "email": email, "password": password},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": LOGIN_URL,
                "Origin": BASE,
            },
            timeout=TIMEOUT,
        )
        return parse_json(resp)

    def sync(self) -> None:
        """登录后访问首页，让服务端会话状态落定。"""
        self.session.get(BASE + "/", timeout=TIMEOUT)

    def sign(self) -> dict:
        resp = self.session.get(
            SIGN_URL,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"},
            timeout=TIMEOUT,
        )
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
                return Outcome(account, ALREADY, sign_msg, points, streak)
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

    outcomes: list[Outcome] = []
    for idx, account in enumerate(accounts, 1):
        print(f"\n[{idx}/{len(accounts)}] 处理 {account.display} ……")
        outcome = run_account(account)
        print(f"   → {STATUS_ICON.get(outcome.status, '?')} {outcome.message}")
        outcomes.append(outcome)
        if idx < len(accounts):
            time.sleep(2)  # 温和一点，避免触发风控

    print_report(outcomes)
    write_step_summary(outcomes)

    bad = [o for o in outcomes if o.status in (FAILED, CAPTCHA, ERROR)]
    if bad:
        print(f"\n❌ {len(bad)} 个账号未成功，退出码 1")
        return 1
    print("\n✅ 全部账号处理完毕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
