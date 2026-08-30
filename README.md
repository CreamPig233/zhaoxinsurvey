# 先锋网络中心 2026 招新报名系统

先锋网络中心 2026 年招新在线报名系统。系统使用 Python 提供 Web 服务，使用 HTML、CSS 和 JavaScript 构建前端，使用 SQLite 保存报名数据和操作日志。

## 功能

- 使用 QQ 号和学号登录，实时读取 `users.csv` 进行身份校验。
- 登录成功后显示姓名、学号和 QQ 号，这些身份信息不可修改。
- 填写性别、部门志愿、调剂意愿、个人优势及未来工作设想，以及其它数码相关特长。
- 部门志愿使用按钮操作：`＋` 加入，`↑` / `↓` 调整顺序，`−` 移除。
- 个人优势及未来工作设想为选填项，提交时使用 UTF-8 Base64 编码传输。
- “你还有其它特长吗？”为选填简答题，提交时单独保存并导出。
- 再次登录后自动加载最近一次提交的问卷。
- 最终问卷按学号覆盖保存，不保留草稿。
- 每次登录尝试写入登录日志，每次提交写入修改日志。
- 提交确认弹窗、成功页、报名截止页和成功页外部跳转按钮。
- 支持电脑和手机浏览器访问。

## 环境要求

- Python 3.9 或更高版本
- 无需安装第三方 Python 包
- 推荐使用现代浏览器，如 Chrome、Edge、Firefox 或 Safari

## 启动

在项目根目录执行：

```powershell
python app.py
```

默认访问地址：

```text
http://127.0.0.1:8000/
```

服务健康检查地址：

```text
http://127.0.0.1:8000/health
```

返回 `{"ok": true}` 说明服务进程正在监听并可以响应请求。

服务使用线程处理请求，并为客户端连接设置 30 秒超时；单个异常或未完成的请求不会阻塞其它访问。修改代码后需要重启正在运行的 `app.py` 进程。

如需修改端口，可设置 `SURVEY_PORT`：

```powershell
$env:SURVEY_PORT = "8080"
python app.py
```

生产环境不建议直接暴露 Python 自带的 `wsgiref` 服务，应在前面配置反向代理、HTTPS 和访问控制。

## 配置

主要配置位于 `app.py` 顶部：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `DEADLINE` | `2026-09-29 12:00:00` | 报名截止时间，使用服务器本地时间 |
| `CSV_PATH` | `users.csv` | 登录名单路径 |
| `DATABASE_PATH` | `data/survey.sqlite3` | SQLite 数据库路径 |
| `CSV_HAS_HEADER` | `False` | CSV 是否包含表头 |
| `SESSION_TTL` | 30 分钟 | 登录会话有效期 |
| `RATE_LIMIT` | 每分钟 20 次 | 登录接口的 IP 限流值 |
| `SUBMISSION_LIMIT` | 每分钟 3 次 | 提交接口的用户/IP 限流值 |

截止时间之后，普通页面显示 `timeend.html`，所有 `/api/` 请求返回 HTTP 410，登录、查看、修改和提交均会被拒绝。

## 登录名单

系统只读取、不修改 `users.csv`。文件默认使用 UTF-8 编码，每行三列，顺序如下：

```csv
QQ号,学号,姓名
10000001,20260001,张三
10000002,,李四
```

默认没有表头。如果实际文件有表头，将 `app.py` 中的 `CSV_HAS_HEADER` 改为 `True`。

登录校验规则：

- QQ 号不存在：提示“请加群后继续。”
- QQ 号存在但学号为空：提示“请群内实名后继续。”
- 学号与名单不完全一致：提示“学号与 QQ 号不匹配，请核对后重试。”
- 每次登录都会重新读取 CSV，因此外部程序更新名单后无需重启服务。

## 数据库

首次启动时自动创建 `data/survey.sqlite3`，包括以下表。`submissions` 表中的 `other_talents` 保存新题目的答案：

- `submissions`：每位用户最近一次完整提交，按学号覆盖保存。
- `login_logs`：成功和失败的登录尝试，包含 QQ 号、学号、时间、IP、User-Agent 和结果。
- `change_logs`：每次提交的前后数据和操作时间，可用于还原修改历史。
- `request_dedup`：保存短期请求编号和内容指纹，用于防止重复提交和重放。

数据库文件包含报名个人信息，应限制文件系统权限，并定期备份。不要将真实名单、数据库或导出文件提交到公开代码仓库。

## 接口

### `POST /api/login`

登录并创建当前浏览器会话。

请求示例：

```json
{
  "qq": "10000001",
  "student_id": "20260001"
}
```

成功响应包含 `name`、`qq`、`student_id` 和提交时使用的 `csrf` token。

### `GET /api/me`

读取当前登录身份和最近一次提交的数据。没有有效会话时返回 HTTP 401。

### `POST /api/submit`

提交问卷。必须携带 `X-CSRF-Token` 请求头。

请求示例：

```json
{
  "gender": "女",
  "departments": [
    "网络技术站 - 网络部",
    "设备服务站 - 硬件部"
  ],
  "transfer": "是",
  "strengths_b64": "",
  "other_talents_b64": "",
  "request_id": "unique-request-id"
}
```

`departments` 至少包含一个部门，最多包含五个不重复的固定部门。`strengths_b64` 和 `other_talents_b64` 都可以为空字符串；后端分别解码后限制为 4000 个字符。

### `POST /api/logout`

校验 CSRF token 后销毁当前会话。

## 导出数据

使用 `export_last_survey.py` 导出当前最终报名数据：

```powershell
python export_last_survey.py
```

默认生成项目根目录下的 `last_survey.csv`。也可以指定数据库和输出文件：

```powershell
python export_last_survey.py `
  --database data/survey.sqlite3 `
  --output last_survey.csv
```

导出内容包括 QQ 号、姓名、学号、性别、五个按优先级排列的部门志愿、调剂意愿、个人优势及未来工作设想、其它特长和填写时间。

## 项目结构

```text
.
├── app.py                         # Web 服务、接口、校验和数据库逻辑
├── export_last_survey.py          # 最终问卷导出工具
├── users.csv                      # 登录名单，只读
├── data/survey.sqlite3            # 运行时生成的数据库
├── templates/
│   ├── login.html                 # 登录页
│   ├── questionnaire.html         # 问卷页
│   ├── success.html               # 提交成功页
│   └── timeend.html               # 截止页
└── static/
    ├── logo.png                   # 系统 Logo
    └── styles.css                 # 页面样式
```

## 安全说明

- 数据库查询使用参数化 SQL，避免 SQL 注入。
- 页面输出和确认弹窗内容经过 HTML 转义，降低 XSS 风险。
- 登录会话保存在服务端内存中，Cookie 使用 `HttpOnly` 和 `SameSite=Lax`，关闭浏览器后不会保留持久化登录状态。
- 提交接口要求会话、CSRF token、有效请求编号和当前截止时间状态。
- 后端重新校验姓名、学号、QQ 号、性别、部门顺序、调剂意愿、个人优势和其它特长内容，不信任前端字段。
- 登录和提交接口具备 IP/用户维度的频率限制。
- `users.csv` 只读，名单更新由外部程序负责。

## 故障排查日志

服务启动后会自动创建 `logs/app.log`，日志文件达到 5 MB 后自动轮转，最多保留 5 个历史文件。日志同时输出到服务进程的控制台。

除 HTTP 200、302 响应和 `/favicon.ico` 的 404 响应外，其余请求都会记录以下信息：

- 请求编号 `request_id`
- 请求方法和路径
- HTTP 状态码
- 请求耗时
- 客户端 IP 和 User-Agent

服务端未捕获异常会记录完整 traceback。接口响应也会带有同一个 `X-Request-ID` 响应头，便于将浏览器错误与服务端日志对应起来。

浏览器端发生网络错误、非 JSON 响应、接口返回非 2xx 状态或页面脚本异常时，会通过 `console.error` 记录详细信息。排查“Failed to fetch”时，可打开浏览器开发者工具的 Console 和 Network 面板，同时查看 `logs/app.log` 中相同 `request_id` 的记录。

## 本地检查

检查 Python 语法：

```powershell
python -m py_compile app.py export_last_survey.py
```
