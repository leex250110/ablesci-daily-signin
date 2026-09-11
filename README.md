# AbleSci 每日自动签到（多账号版）

在 GitHub Actions 上自动完成 [ablesci.com](https://www.ablesci.com) 的每日签到，**一个仓库可以同时服务多个人**。

加一个人只需要两步：在仓库里加 2 个 Secret，然后在 workflow 里加 2 行。不需要服务器，不需要写代码，也不需要对方有 GitHub 账号。

---

## ⚠️ 先看这个：旧版脚本已经失效了

如果你之前跑的是原版脚本，它**大概率一直在静默失败**，原因有两个：

**1. CSRF 正则匹配不到，脚本直接退出**

站点改版后，登录页的标记变成了这样：

```html
<input type="hidden" id="login2-csrf" name="_csrf" value="t5zEo1ywa4_3Qv...">
<meta name="csrf-token" content="t5zEo1ywa4_3Qv...">
```

旧脚本找的是 `id="csrf-val" value="..."`，早已不存在。匹配失败后脚本会打印
`Error: Could not find CSRF token` 并 `sys.exit(1)` —— **连登录请求都没发出去**。

**2. CSRF token 每次请求都会轮换，用错会被要求输入验证码**

实测三组对照：

| 提交的 `_csrf` | 站点返回 |
| --- | --- |
| 当次从表单抓取的正确 token | `{"code":1,"msg":"邮箱或密码错误","verify":0}` ✅ 正常校验账密 |
| `_csrf` cookie 里的值 | `{"code":1,"msg":"请输入验证码","verify":1}` ⚠️ |
| 空字符串 | `{"code":1,"msg":"请输入验证码","verify":1}` ⚠️ |

所以**每次签到都必须重新 GET 登录页、重新抓 token**，不能复用。本版已按此实现，
并会自动识别"被要求验证码"这种状态、给出明确提示。

---

## 🚀 快速开始

### 第 1 步：添加账号

打开仓库的 Secrets 页面：

```
https://github.com/leex250110/ablesci-daily-signin/settings/secrets/actions
```

点 **New repository secret**，添加两组值（注意名字要完全一致）：

| Name | Secret |
| --- | --- |
| `ABLESCI_1_EMAIL` | 对方的 AbleSci 登录邮箱 |
| `ABLESCI_1_PASSWORD` | 对方的 AbleSci 登录密码 |

**要加第二个人**，就再加 `ABLESCI_2_EMAIL` / `ABLESCI_2_PASSWORD`，以此类推，最多支持 20 个。

> 密码只在 GitHub 的加密 Secret 里，日志和代码里都不会出现。
> 一旦填进去，连你自己也无法再查看，只能覆盖重写。

### 第 2 步：在 workflow 里放行这个槽位

打开 `.github/workflows/ablesci-signin.yaml`，找到 `env:` 段落。
默认已经预留了 **5 个槽位**（`ABLESCI_1_*` 到 `ABLESCI_5_*`），所以加前 5 个人**这步可以跳过**。

如果要加第 6 个及以后，照抄两行即可：

```yaml
      ABLESCI_6_EMAIL: ${{ secrets.ABLESCI_6_EMAIL }}
      ABLESCI_6_PASSWORD: ${{ secrets.ABLESCI_6_PASSWORD }}
```

没配置的槽位会变成空字符串，脚本会自动跳过，不会报错。

### 第 3 步：手动跑一次验证

打开 Actions 页面：

```
https://github.com/leex250110/ablesci-daily-signin/actions
```

选左边 **「AbleSci 每日签到」** → 右上角 **Run workflow** → 绿色按钮。

大约 30 秒后刷新，点进这次运行记录，你会看到：

- **签到结果**（Job Summary，最直观）—— 一张表格，列出每个账号的状态、积分、连签天数
- **执行签到**（日志）—— 详细过程

看到下面的汇总就是成功了：

```
✅ 账号1 (zh***@qq.com) —— 签到成功
   当前积分：1234
   连续签到：7 天
```

### 第 4 步：什么都不用做了

之后每天北京时间 **08:00** 会自动执行，结果记录在 Actions 页面。

---

## 📊 结果状态说明

| 图标 | 状态 | 含义 | 要不要管 |
| --- | --- | --- | --- |
| ✅ | 签到成功 | 本次签到完成 | 不用 |
| ℹ️ | 今日已签到 | 今天已经签过了 | 不用 |
| ❌ | 失败 | 密码错、账号不存在等 | **要**，检查 Secret 是否填对 |
| 🤖 | 被要求验证码 | 站点要求人机验证 | **要**，见下方 FAQ |
| ⚠️ | 异常 | 网络问题、站点改版等 | 看说明文字 |

只要有任何账号不是 ✅ 或 ℹ️，整个任务就会**变红**，方便你一眼发现。

---

## ❓ 常见问题

<details>
<summary><b>出现「被要求验证码」怎么办？</b></summary>

说明这个 IP 或账号短时间内失败次数太多，触发了风控。处理方式：

1. 用浏览器正常登录一次 ablesci.com，手动完成验证码
2. 确认密码没改过、Secret 填的是对的
3. 回到 Actions 页面重新 Run workflow

如果反复出现，通常意味着密码本身就错了 —— 先确认能手动登录成功。
</details>

<details>
<summary><b>为什么定时任务没准点跑？</b></summary>

GitHub 免费版的 cron **不保证准点**，高峰期可能延迟几分钟到半小时以上，
极端情况下会直接跳过某一天。这是 GitHub 的限制，不是脚本的问题。

如果你需要精确时间，可以考虑改用 Cloudflare Workers 的 Cron Triggers（免费、零冷启动）。
</details>

<details>
<summary><b>跑了一段时间后突然不跑了？</b></summary>

GitHub 会在**仓库连续 60 天没有任何活动**时自动暂停定时任务。
签到本身不算"活动"，所以需要你偶尔动一下仓库 —— 随便改个文件、提交一次，
或者到 Actions 页面手动 Run 一次即可重新激活。
</details>

<details>
<summary><b>对方修改了密码怎么办？</b></summary>

到 Secrets 页面点开对应的 `ABLESCI_N_PASSWORD` → **Update**，填入新密码。
Secret 无法查看，只能覆盖。
</details>

<details>
<summary><b>站点又改版导致脚本失效怎么办？</b></summary>

workflow 里有一个**「站点自检」**步骤，它不登录、只验证能否抓到 CSRF token。

如果某天它失败了，说明站点结构又变了。日志里会明确告诉你
「站点可能再次改版，请更新 ablesci_signin.py 里的 CSRF_PATTERNS」，
去那里补一条正则即可。
</details>

---

## 🔒 安全说明

- **密码存放**：只存在于 GitHub 加密 Secret 中，仓库代码和提交历史里没有密码。
- **日志打码**：所有输出里的邮箱都会打码（`zhangsan@qq.com` → `zh***@qq.com`）。
  这一点很重要，因为**公开仓库的 Actions 日志是所有人可见的**。
  请顺带确认一下本仓库的可见性：Settings → General → Danger Zone。
- **权限最小化**：workflow 只声明了 `contents: read`。
- **风险提示**：把密码交给任何自动化服务都意味着你信任它。本项目把密码放在你自己
  拥有的仓库里，不经过第三方服务器；但如果你对这点有顾虑，让对方自己
  fork 一份仓库、自己填 Secret 是最稳妥的。

---

## 🖥️ 本地运行

除了 GitHub Actions，也可以在本地直接跑（适合调试）：

```bash
pip install -r requirements.txt

# 只验证站点可达、CSRF 能抓到，不需要账号
python ablesci_signin.py --check

# 实际签到（Windows PowerShell 写法）
$env:ABLESCI_1_EMAIL="邮箱"; $env:ABLESCI_1_PASSWORD="密码"
python ablesci_signin.py

# Linux / macOS
ABLESCI_1_EMAIL="邮箱" ABLESCI_1_PASSWORD="密码" python ablesci_signin.py
```

退出码：全部成功为 `0`，任一账号失败为 `1`。

---

## 📁 文件结构

```
ablesci_signin.py                          # 签到主程序
requirements.txt                           # 依赖（只有 requests）
.github/workflows/ablesci-signin.yaml      # 定时任务定义
```

---

## 附：工作原理

```
GET  /site/login      →  抓取当次有效的 _csrf token
POST /site/login      →  提交 email + password + _csrf，返回 JSON
GET  /                →  同步会话状态
GET  /user/sign       →  执行签到，返回 JSON
GET  /                →  读取积分与连续签到天数
```
