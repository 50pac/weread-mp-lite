# weread-mp-lite

把你在微信读书（weread.qq.com）里能看到的**微信公众号**近期文章，导出成 Markdown 文件和 RSS 2.0 订阅源。

填写一份 Cookie 和一份公众号名称列表，然后运行一条命令即可。程序会：

1. 把公众号名称解析成 `MP_WXS_…` 形式的 bookId（先搜网页；若搜索只返回图书，再从书架匹配；也可直接填 `book_id` 或带 `__biz` 的文章链接）；
2. 调用 `/web/mp/articles?bookId=…&offset=…` 列出近期文章；
3. 打开文章原文链接抓取正文 HTML（原文页没有正文时，改用 `/web/mp/content?reviewId=…`）；
4. 写成带 front matter 的 Markdown，并生成每个公众号一份 RSS，外加一份汇总 RSS。

已经下载过的文章记在状态文件里，下次运行会跳过。接口地址都集中在 `weread_mp_lite/client.py` 的 `ENDPOINTS` 里，微信读书改版时只改那一处。

## 环境

- Python 3.9 或更高版本
- 一个已登录的微信读书网页 Cookie（本仓库不提供、也不内置任何账号）

## 安装

在项目目录中执行。

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

如果执行策略阻止激活脚本，可以先运行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

也可以不激活，直接用 `.venv\Scripts\python`。

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

## 获取微信读书 Cookie

Cookie 是登录凭证。泄露后别人可以访问你的微信读书账号。不要提交到 Git，不要发到聊天工具。

1. 用 Chrome 或 Edge 打开 <https://weread.qq.com> ，扫码登录。
2. 登录后随便点开一个页面，让站点发出请求（公众号页、书架页都可以）。
3. 按 `F12` 打开开发者工具，切到 **Network（网络）** 面板。
4. 勾选 **Preserve log**，然后刷新页面。
5. 在请求列表里点一条发往 `weread.qq.com` 的请求。过滤框可以填 `weread.qq.com`。常见的有 `/web/shelf/sync`、`/api/mp/cover`、`/web/search/global`。
6. 在右侧 **Headers（标头）** → **Request Headers** 里找到 `Cookie`。
7. 复制整段值。它通常包含 `wr_vid`、`wr_skey`、`wr_rt` 等字段，中间用分号隔开。不要只复制其中一个字段。行首的 `Cookie:` 四个字可以带上，程序会去掉。

## Cookie 放在哪里

三选一，优先级从高到低：

1. `config.yaml` 的 `cookie` 字段（不建议，容易被误提交）。
2. 环境变量 `WEREAD_COOKIE`。
3. `cookie.txt`：放在配置文件同目录，或放在运行命令时的当前目录。文件里只放这一行 Cookie。

PowerShell（仅当前窗口有效）：

```powershell
$env:WEREAD_COOKIE = "wr_vid=...; wr_skey=...; wr_rt=..."
```

macOS / Linux：

```bash
export WEREAD_COOKIE='wr_vid=...; wr_skey=...; wr_rt=...'
```

`config.yaml`、`cookie.txt`、`*.cookie` 和 `.env` 已在 `.gitignore` 中。

## 配置

编辑 `config.yaml`：

```yaml
cookie: ""

accounts:
  - 示例公众号
  # - name: 另一个公众号
  #   book_id: MP_WXS_0000000000

max_articles: 10
request_delay: 2
output_dir: output
```

| 字段 | 含义 |
| --- | --- |
| `cookie` | 登录 Cookie。留空或仍是「请在这里粘贴」这类占位文案时，使用环境变量或 `cookie.txt`。写成完整 Cookie 时优先用这里的值。 |
| `accounts` | 公众号名称列表。名称要和微信读书里显示的一致。写成 `MP_WXS_…` 或带 `book_id` 时跳过搜索。 |
| `max_articles` | 每个公众号最多抓取多少篇近期文章。 |
| `request_delay` | 两次 HTTP 请求之间等待的秒数。请保持在 1 秒以上。 |
| `output_dir` | 输出目录。相对路径相对于当前工作目录。 |

可选字段：`retries`（默认 3）、`timeout`（默认 20 秒）。

## 运行

```powershell
python -m weread_mp_lite
python -m weread_mp_lite --config config.yaml --limit 5
python -m weread_mp_lite --dry-run
```

| 参数 | 含义 |
| --- | --- |
| `--config` | 配置文件路径，默认 `config.yaml`。 |
| `--limit` | 覆盖 `max_articles`，限制每个公众号的篇数。 |
| `--dry-run` | 只搜索并列出将要下载的文章，不写文件、不更新状态。 |

`--dry-run` 仍会请求搜索和文章列表，但不会请求正文，也不会创建 `output/`。

## 输出

```text
output/
  .state.json                 已下载文章（按 reviewId）。删除某条后才会重新下载
  示例公众号/
    2024-05-01_文章标题.md
  rss/
    示例公众号.xml            该公众号的 RSS 2.0（全文在 content:encoded）
    all.xml                   所有公众号的汇总
```

每篇 Markdown 的开头是 YAML front matter：`title`、`author`（公众号名）、`date`（北京时间）、`source`（原文链接），另外还有 `book_id` 和 `review_id` 供去重。正文是由 HTML 转成的 Markdown。

RSS 是 RSS 2.0，条目里的 `content:encoded` 是 Markdown 全文。阅读器会把它当作文本显示。

## 工作原理

微信读书把公众号当成一本书，bookId 形如 `MP_WXS_<数字>`。本工具复用网页版接口（与开源项目 [we-mp-rss](https://github.com/rachelos/we-mp-rss) 的微信读书公众号采集相同的思路）：

| 步骤 | 方法 | 地址 |
| --- | --- | --- |
| 搜索公众号 | GET | `https://weread.qq.com/web/search/global?keyword=…&scope=2` |
| 文章列表 | GET | `https://weread.qq.com/web/mp/articles?bookId=…&offset=…` |
| 正文 | GET | 文章原文 `https://mp.weixin.qq.com/s/<token>` |
| 正文备用 | GET | `https://weread.qq.com/web/mp/content?reviewId=…` |

`offset` 按返回的群发组数量前进，通常一页 20 组；一组里的 `subReviews` 会全部展开。

若文章列表返回错误码 `-2041`，程序会改请求 `https://weread.qq.com/api/mp/cover?bookId=…`。该接口只能拿到最新一篇，终端里会有说明。

微信读书有时把原文 token 里的 `_` 写成 `~`。程序会把它换回 `_`，否则 `mp.weixin.qq.com` 会报参数错误。

## 常见问题

### Cookie 过期了怎么办？

终端会直接说明 Cookie 无效或已过期（常见错误码 `-2012`、`-2010`，或 HTTP 401/403）。重新登录 weread.qq.com，按上面的步骤复制新 Cookie，替换 `cookie.txt` 或环境变量后再运行。网页 Cookie 通常几天到几周就会失效，这是微信读书的正常行为。

### 提示请求太频繁，或出现 -2041？

把 `request_delay` 调到 2–5 秒，减小 `max_articles`，隔一段时间再跑。不要把间隔改成 0。程序对 429 和 5xx 会按 `retries` 重试，登录失效不会重试。

### 搜不到公众号？

名称必须和微信读书中的公众号名称一致。搜索结果不唯一时，程序会列出候选并停止，不会猜一个错误的号。此时把 bookId 写进配置：

```yaml
accounts:
  - name: 你的公众号
    book_id: MP_WXS_1234567890
```

bookId 可以在微信读书公众号页面的网络请求里看到（参数名 `bookId`）。

### 想重新下载某一篇？

删掉对应的 Markdown 文件，并删掉 `output/.state.json` 里该 `reviewId` 的记录。只删文件、留下状态记录时，下次也会重新下载。文件还在、状态文件丢了，则按文件头里的 `review_id` 跳过。

### 测试

测试全部使用伪造的 HTTP 响应，不需要真实 Cookie，也不会访问微信读书：

```powershell
pip install pytest
pytest -q
```

## 免责声明

本工具仅供个人学习，用来整理你自己已经可以在微信读书中阅读的内容。请遵守微信读书与微信公众平台的服务条款，尊重作者版权。不要用它批量公开传播、转售或绕过付费与访问控制。程序不会识别验证码，也不会试图绕过风控。使用者自行承担账号限制、Cookie 失效和版权风险。

## 许可证

[MIT](LICENSE)
