# ugreen-ai-reply

绿联 NAS 社区自动回复脚本的重写版：**每个板块各取最新的几篇**，读标题和正文，交给大模型挑选出真正值得回的内容，再以真实用户的口吻回复；没有可补充的技术点时，也允许只回一个表情。适用于青龙面板定时任务，也可本地直接跑。

原始仓库：[silence9527/ugreen_auto_reply](https://github.com/silence9527/ugreen_auto_reply)

---

## 一、先说清楚：默认是演练模式

**默认 `UGREEN_DRY_RUN=1`，只抓取、只生成、只打印，绝不发帖。**

确认报告和回复内容都满意后，再设置 `UGREEN_DRY_RUN=0` 才会真正发送。自动在公共社区发内容是有后果的（见文末"合规提醒"），这个默认值是故意的。

---

## 二、修了原项目的哪些问题

| 问题 | 原脚本 | 本工程 |
| --- | --- | --- |
| 抓帖子 | `a[href*="thread-"]`，只命中页脚 3 个伪静态链接（tid 29 社区公约 / 30 隐私政策 / 104 侵权投诉指引），一页 80+ 个真实帖子全漏 | 以**各板块 RSS** 为主（`mod=rss&fid=x&auth=0`，实测实时且 pubDate 精确到秒），列表页仅作兜底且限定在 `#threadlisttableid` 容器内，避开 `#layer_bbrules` 隐藏层和页脚 |
| 板块 | 3 个（fid 10/9/13） | 6 个：功能讨论 10、玩法攻略 9、用户互助 13、Docker 14、社区活动 83、新手入门 12 |
| 选帖 | 全部候选随机抽 20 次，容易反复打在同一帖 | **每个板块各取最新 N 篇**（默认 4），按板块轮流交错，保证六个板块都有机会；再按时间窗口、黑名单、已回复记录过滤 |
| 回复内容 | 36 条固定语料随机抽，与帖子内容无关 | 大模型读标题 + 首楼正文后生成，并先判断 `should_reply`；纯签到/灌水/公告帖直接跳过 |
| 回复形态 | 只能发固定长句 | 文字回复（`text`，30~120 字）或**表情回复**（`emoji`，纯表情 / 表情加 6 字以内短语）；表情同样过质检，套话、外链、AI 自述、连续刷同一表情都会被拦 |
| 成功判定 | `status_code == 200` 就算成功（Discuz 拒绝回复也是 200） | 解析响应体，先匹配失败标记再匹配成功标记，都不命中一律按失败处理；标记可用环境变量覆盖 |
| 去重 | 无，20 次 `random.choice` 会反复刷同一帖 | 状态文件记录已回复 tid；官方制度帖 tid 与标题黑名单；每次回复条数上限；随机间隔 |
| 回复地址 | 手工拼 `mod=post&action=reply` URL，缺 fid/extra | 直接取详情页 `form#fastpostform` 的 action；formhash 取 `input[name=formhash]` |
| 企业微信推送 | 把 `QYWX_AM`（corpid 四段）当成 PushPlus 的 token 拼进 `qyapi.pushplus.plus`，必然失败 | 依次走青龙 `sendNotify` → `QYWX_KEY` 群机器人 → `QYWX_AM` 应用消息（按青龙标准格式 `corpid,corpsecret,touser,agentid`，并兼容写反的顺序） |
| 统计 | 关键词命中后风格变量被永久改成"技术型"，汇总失真 | 按帖逐条记录 AI 判断 / 质检 / 发送结果 |
| 耗时 | 20 条 × 30~600 秒，最长 3 小时以上 | 默认最多 5 条，间隔 60~180 秒，详情抓取上限 20 篇 |

---

## 三、快速开始

### 本地

```bash
cd ugreen-ai-reply
pip install -r requirements.txt

# 演练：不需要 Cookie、不需要 API Key，会抓真实帖子并用本地模板演示
python ugreen_ai_reply.py --hours 24

# 接入大模型再看效果（仍然不发帖）
export AI_API_KEY=sk-xxx
python ugreen_ai_reply.py --hours 24

# 先看生效配置（Key 脱敏），再单独测一次模型连通性
python ugreen_ai_reply.py --show-config
python ugreen_ai_reply.py --test-ai

# 只看某几个板块，每个板块取最新 2 篇，最多拟回复 3 条
python ugreen_ai_reply.py --fids 14,10 --hours 48 --per-forum 2 --limit 3
```

### 真正发帖（本地或青龙）

```bash
export UGREEN_COOKIE='登录后 F12 复制的完整 Cookie'
export AI_PROVIDER=deepseek       # 一行选定 baseURL + 模型；也可直接配 AI_BASE_URL / AI_MODEL
export AI_API_KEY='sk-xxx'
export UGREEN_DRY_RUN=0
python ugreen_ai_reply.py --test-ai   # 先确认模型通
python ugreen_ai_reply.py
```

### 青龙面板

1. 把 `ugreen_ai_reply.py` 放进青龙 `scripts` 目录（订阅管理里填 raw 链接亦可）。
2. 依赖管理 → Python3 添加 `requests`、`beautifulsoup4`（`lxml` 可选，装上解析更快）；脚本缺依赖时也会尝试自动装一次。
3. 环境变量见下表，建议至少配 `UGREEN_COOKIE`、`AI_API_KEY`、`UGREEN_DRY_RUN`。
4. 定时任务：`task ugreen_ai_reply.py`，建议 `0 9,20 * * *` 之类低频规则。
5. 通知：青龙已配好企业微信时无需额外设置，脚本会自动调用 `sendNotify`；也可单独配 `QYWX_KEY` 或 `QYWX_AM`。

---

## 四、环境变量

### 抓取与发送

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `UGREEN_COOKIE` | 空 | 登录后的完整 Cookie；**实发模式必填** |
| `UGREEN_DRY_RUN` | `1` | `0` 才真正发帖 |
| `UGREEN_FORUM_FIDS` | `10,9,13,14,83,12` | 板块 fid，逗号分隔 |
| `UGREEN_HOURS` | `24` | 只看最近 N 小时的新帖；实发模式下会优先沿用状态文件里的上次水位。命令行 `--hours` 可覆盖 |
| `UGREEN_PER_FORUM_LIMIT` | `4` | **每个板块各取最新几篇**；六个板块按轮转交错，谁都不会被挤掉。命令行 `--per-forum` 可覆盖 |
| `UGREEN_ALLOW_EMOJI_REPLY` | `1` | 允许表情回复；`0` 或 `--no-emoji` 只发文字回复 |
| `UGREEN_MAX_DETAIL` | `30` | 最多抓多少篇详情页（正文 + formhash），需不小于板块数 × 每板块篇数 |
| `UGREEN_MAX_REPLIES` | `5` | 每次运行最多发几条（含表情回复），是上限不是固定值。命令行 `--limit` 可覆盖 |
| `UGREEN_MIN_DELAY` / `UGREEN_MAX_DELAY` | `60` / `180` | 两条回复之间的随机间隔（秒） |
| `UGREEN_REQUEST_DELAY` | `1.0` | 抓取请求之间的间隔（秒），别调太低 |
| `UGREEN_TIMEOUT` | `30` | 论坛请求超时（秒） |
| `UGREEN_STATE_DIR` | 脚本目录 | 状态文件 `ugreen_state.json` 存放位置 |
| `UGREEN_SKIP_EVENTS` | `0` | `1` 时跳过 fid=83 社区活动帖 |
| `UGREEN_USE_LIST_FALLBACK` | `1` | 某板块 RSS 返回 0 条时是否改用列表页兜底 |
| `UGREEN_LOCAL_FALLBACK` | `1` | 仅控制演练模式是否用本地模板演示；**实发模式强制要求 `AI_ENABLED=1` 且至少一个 apiKey 变量有值**，模板与帖子内容无关，不允许用它发帖 |
| `UGREEN_NOTIFY` | `1` | `0` 关闭推送 |
| `UGREEN_MARK_STATE` | `1` | `0` 不写状态文件；演练模式默认也不写水位，除非显式 `--mark` |
| `UGREEN_DEBUG` | `0` | `1` 时打印 Discuz 回复响应体片段，便于排查 |
| `UGREEN_SUCCESS_MARKERS` | `succeedhandle,回复发布成功,帖子回复成功,发布成功` | 成功标记，逗号分隔 |
| `UGREEN_FAIL_MARKERS` | 见脚本 | 失败标记，逗号分隔 |
| `UGREEN_AUTO_INSTALL_DEPS` | `1` | 缺依赖时是否自动 pip 安装 |

### AI 大模型（OpenAI 兼容接口）

**最小配置只要三个变量**：`AI_BASE_URL`、`AI_API_KEY`、`AI_MODEL`。所有变量都可在青龙面板逐条添加，也可本地 `export`；命令行同名参数优先级更高。

#### 核心三项与兼容别名

同名含义的变量按下面顺序取**第一个非空值**，方便复用青龙里已有的变量（比如别的脚本用的 `OPENAI_API_KEY`）。

| 用途 | 推荐变量 | 兼容别名（按优先级） |
| --- | --- | --- |
| baseURL | `AI_BASE_URL` | `UGREEN_AI_BASE_URL` → `OPENAI_BASE_URL` → `OPENAI_API_BASE` → `LLM_BASE_URL` → `AI_API_BASE` → `SILICONFLOW_BASE_URL` → `DEEPSEEK_BASE_URL` |
| apiKey | `AI_API_KEY` | `UGREEN_AI_API_KEY` → `OPENAI_API_KEY` → `LLM_API_KEY` → `AI_TOKEN` → `SILICONFLOW_API_KEY` → `SF_API_KEY` → `DEEPSEEK_API_KEY` → `API_KEY` |
| model | `AI_MODEL` | `UGREEN_AI_MODEL` → `OPENAI_MODEL` → `LLM_MODEL` → `AI_MODEL_NAME` → `SILICONFLOW_MODEL` → `SF_MODEL` → `DEEPSEEK_MODEL` |

默认值：`AI_BASE_URL=https://api.siliconflow.cn/v1`、`AI_MODEL=deepseek-ai/DeepSeek-V3`、Key 为空。日志会打印当前生效的模型和 Key 来源（**Key 脱敏**，只显示首尾几位），青龙里排查变量有没有生效很方便。

#### 厂商预设与地址规则

`AI_PROVIDER` 可一键填好 baseURL + 模型，**显式变量始终优先于预设**：

| `AI_PROVIDER` | 自动填入的 baseURL / model |
| --- | --- |
| `siliconflow`（默认） | `https://api.siliconflow.cn/v1` / `deepseek-ai/DeepSeek-V3` |
| `deepseek` | `https://api.deepseek.com/v1` / `deepseek-chat` |
| `openai` | `https://api.openai.com/v1` / `gpt-4o-mini` |
| `moonshot` | `https://api.moonshot.cn/v1` / `moonshot-v1-8k` |
| `dashscope` | `https://dashscope.aliyuncs.com/compatible-mode/v1` / `qwen-plus` |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` / `glm-4-flash` |
| `ollama` / `lmstudio` | 本地 `http://127.0.0.1:11434/v1` / `1234/v1` |
| `custom` | 不填，完全由自己的变量决定 |

baseURL 会自动规范化，下面几种写法都能用：

```
https://api.deepseek.com              → 自动补 /v1
https://api.deepseek.com/v1           → 原样保留
https://dashscope.aliyuncs.com/compatible-mode/v1  → 自定义路径原样保留
https://api.deepseek.com/v1/chat/completions      → 去掉尾部再拼，不会重复
```

最终 POST 地址 = `baseURL + "/" + AI_CHAT_PATH`，默认即 `.../v1/chat/completions`。若端点带 deployment 或 query（Azure、自建网关），直接用 `AI_ENDPOINT` 指定完整地址，它优先级最高、跳过拼接。

#### 其余可调参数

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_ENDPOINT` / `AI_CHAT_URL` / `OPENAI_CHAT_URL` / `LLM_ENDPOINT` | 空 | 完整 chat 地址，优先于 baseURL 拼接 |
| `AI_CHAT_PATH` | `chat/completions` | 自定义路径后缀 |
| `AI_AUTH_HEADER` | `Authorization` | 认证头名，Azure 可设 `api-key` |
| `AI_AUTH_SCHEME` | `Bearer` | **置空**则直接把 Key 作为该头的值（不拼 Bearer） |
| `AI_TIMEOUT` | `120` | 模型请求超时（秒） |
| `AI_TEMPERATURE` | `0.6` | 采样温度，限制在 0~2 |
| `AI_MAX_TOKENS` | `2400` | 单次最大输出 token |
| `AI_TOP_P` | 不传 | 设了才加入请求体 |
| `AI_BATCH_SIZE` | `5` | 每批送几篇给模型 |
| `AI_RETRIES` | `3` | 失败/JSON 解析失败重试次数（1~6） |
| `AI_JSON_MODE` | `1` | 请求 `response_format=json_object`；端点返回 400 不支持时自动去掉并重试 |
| `AI_ENABLED` | `1` | `0` 关闭 AI；实发模式下会拒绝发帖（防止用模板灌水） |
| `AI_EXTRA_BODY` | `{}` | JSON 对象，追加厂商私有参数或覆盖任一请求字段 |
| `AI_EXTRA_HEADERS` | `{}` | JSON 对象或 `K: V; K2: V2`，用于 OpenRouter 等需要额外头的平台 |
| `AI_SYSTEM_PROMPT` | 内置 | 整段替换系统提示词 |
| `AI_SYSTEM_PROMPT_FILE` | 空 | 从文件读提示词（青龙里可挂载） |
| `AI_PROMPT_EXTRA` | 空 | 在提示词末尾追加自定义要求 |

命令行对应参数：`--base-url`、`--api-key`、`--model`、`--endpoint`；另有 `--show-config`（打印生效配置，Key 脱敏）和 `--test-ai`（发一条最小请求验证连通性），两者都不访问社区、不发帖。

#### 常见配置示例

```bash
# 硅基流动（默认，只需 Key）
AI_API_KEY=sk-xxx

# DeepSeek 官方
AI_PROVIDER=deepseek
AI_API_KEY=sk-xxx

# 本地 Ollama（无需真 Key，随便填个非空串）
AI_BASE_URL=http://127.0.0.1:11434/v1
AI_MODEL=qwen2.5:14b
AI_API_KEY=ollama

# OpenRouter：自定义头 + 关闭思考模式
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=deepseek/deepseek-chat
AI_API_KEY=sk-or-xxx
AI_EXTRA_HEADERS={"HTTP-Referer":"https://example.com","X-Title":"ugreen"}
AI_EXTRA_BODY={"reasoning":{"enabled":false}}

# 自检
python ugreen_ai_reply.py --show-config
python ugreen_ai_reply.py --test-ai
```

### 通知

| 变量 | 格式 |
| --- | --- |
| `QYWX_KEY` | 企业微信群机器人 key |
| `QYWX_AM` | `corpid,corpsecret,touser,agentid`（青龙标准格式，可含第 5 段；脚本兼容 agentid/touser 写反的情况） |

---

## 五、一次运行的流程

```
六个板块各自 RSS ──> 按 tid 去重 ──> 时间窗口 ──> 黑名单/已回复过滤
   │
   ├─ 每个板块各取最新 N 篇，按板块轮转交错（默认 6 × 4 = 24 篇）
   │
   ├─ 详情页：首楼正文、标题、formhash、form#fastpostform 的 action
   │
   ├─ AI 批量挑选有意义的帖子：should_reply / kind / reply / reason
   │
   ├─ 质检：文字回复 8~200 字；表情回复 ≤20 字且必须含表情；
   │        两者都拦套话、外链联系方式、AI 自述、连续重复
   │
   └─ 发帖（或演练）──> 解析响应体判定成败 ──> 成功才写入状态 ──> 汇总 + 推送
```

### 每次会回复多少条

日志里的 `回复上限=5` 是**全局天花板，不是每次固定发 5 条**。数量按下面几层漏斗逐层收窄（括号里是一次实测值）：

| 阶段 | 数量 | 决定因素 |
| --- | --- | --- |
| 六板块 RSS | 6 × 20 = 120 篇 | 论坛每个板块 RSS 固定返回约 20 条 |
| 时间窗口 + 黑名单 + 已回复 | 11 篇 | `UGREEN_HOURS`（默认 24）、官方制度帖黑名单、状态文件里的已回复记录 |
| 每板块各取最新 N 篇并交错 | 9 篇 | `UGREEN_PER_FORUM_LIMIT`（默认 4）；某板块窗口内不足 4 篇就按实际数取，为 0 时不出现 |
| AI 判断 + 内容质检 | 0~9 篇 | `should_reply=false` 跳过签到/灌水/公告；套话、外链联系方式、AI 自述、重复回复再被质检拦下 |
| **实际发送** | **0~5 条** | `UGREEN_MAX_REPLIES`（默认 5），文字与表情回复都计入；**只有发送成功才计数**，失败会继续试后面的帖子 |

即 **实际条数 = min(上限, 通过 AI 判断与质检的帖子数)**，社区没新帖时为 0 条，按当前活跃度常见 1~3 条。注意 `每板块最新 4 篇` 只是**候选配额**，用来保证六个板块送入 AI 的顺序公平，**不是回复配额**——某板块的帖子被判不值得回，它就是 0 条。

想调数量：

| 变量 / 参数 | 效果 |
| --- | --- |
| `UGREEN_MAX_REPLIES=3` 或 `--limit 3` | 每次最多 3 条 |
| `UGREEN_PER_FORUM_LIMIT=2` 或 `--per-forum 2` | 每板块候选最多 2 篇，总量变小 |
| `UGREEN_HOURS=48` 或 `--hours 48` | 放宽时间窗口，候选变多，才有机会接近上限 |
| `UGREEN_SKIP_EVENTS=1` | 跳过 fid=83 社区活动帖，省掉大概率被判无效的候选 |

实发时每两条回复之间随机等 `UGREEN_MIN_DELAY`~`UGREEN_MAX_DELAY`（默认 60~180 秒，最后一条后不等待），5 条大约 4~12 分钟，青龙任务的超时设置要留够；演练模式不做这个等待，所以很快。

### 每个板块取最新，而不是全局取最新

跨板块合并后如果直接按时间截断，结果会变成"哪个板块更新得快就全是它的"。所以脚本按 `fid` 分组，每板块各取最新 `UGREEN_PER_FORUM_LIMIT` 篇，再按板块轮转交错送进 AI 判断。日志会逐行打印每个板块取了几篇、最新一篇是什么，方便核对；`UGREEN_MAX_DETAIL` 要留够 `板块数 × 每板块篇数`，默认 30 足够。

### 挑选"有意义"的回复 + 表情回复

模型输出严格 JSON，每篇帖子一条：

```json
{"results":[
  {"tid":"7342","should_reply":true,"kind":"text",
   "reply":"Vivo 的这个桌面快捷方式是系统权限控制的，可以先在设置里把「创建桌面快捷方式」权限打开…",
   "reason":"具体机型的具体问题"},
  {"tid":"7323","should_reply":true,"kind":"emoji",
   "reply":"👍","reason":"教程很完整，无可补充"},
  {"tid":"7339","should_reply":false,"kind":"text","reply":"",
   "reason":"纯反馈无信息量"}
]}
```

系统提示词明确要求：文字回复必须紧扣帖子的具体内容、不编造功能与版本、不假装官方、不留外链联系方式、30~120 字、不写"感谢分享/支持楼主"这类套话；信息不足时只追问一个关键信息。不值得回的帖子直接 `should_reply=false`。

`kind="emoji"` 用于"帖子写得很完整、没有可补充的技术点，但确实值得鼓励"的情况，允许纯表情（`👍`）、论坛表情码（`[赞]`）或"表情 + 6 字以内短语"（"这个思路很巧 👏"）。质检会放宽长度，但**仍然拦截**灌水套话、外链、联系方式、AI 自述，以及连续三次刷同一个表情。不想用表情时设 `UGREEN_ALLOW_EMOJI_REPLY=0`。

---

## 六、常见问题

**Q：日志说"RSS 汇总 0 篇"？**
先确认 `https://club.ugnas.com/forum.php?mod=rss&fid=10&auth=0` 能否打开。RSS 关闭时脚本会自动走列表页兜底（`UGREEN_USE_LIST_FALLBACK=1`）。

**Q：实发模式提示"Cookie 无效或已过期"？**
浏览器重新登录，`F12 → Network → 任意请求 → Request Headers → Cookie` 整串复制。Cookie 一般几十天过期，建议定期更新。

**Q：明明发帖成功，日志却说"响应中未找到成功标记"？**
Discuz 模板不同，成功文案可能变化。设 `UGREEN_DEBUG=1` 看响应体片段，把其中的成功字样加进 `UGREEN_SUCCESS_MARKERS`。**不要**简单把这条改成"只要 200 就算成功"——那正是原脚本误报的根源。

**Q：模型连不上 / 404 / 401，怎么知道青龙里的变量有没有生效？**
先跑 `python ugreen_ai_reply.py --show-config`，它会打印最终 endpoint、model、baseURL 和 Key 来自哪个环境变量（Key 脱敏）；再跑 `--test-ai` 单独发一条最小请求。常见原因：baseURL 漏 `/v1`（脚本会自动补）、把完整 `/chat/completions` 又拼了一次（脚本会去重）、`AI_ENABLED=0`、或某个优先级更高的别名变量（如 `API_KEY`）覆盖了真正想用的 Key。带 deployment/query 的端点请直接设 `AI_ENDPOINT`。

**Q：AI 一直返回不合法 JSON？**
换一个对 `response_format` 支持更好的模型，或设 `AI_JSON_MODE=0`；脚本会自动去掉 Markdown 围栏并按 `AI_RETRIES` 重试。

**Q：表情回复被论坛拒了？**
部分 Discuz 配置会限制过短内容，响应里出现"内容太短"时脚本会如实记为失败（这比原脚本误报成功要好）。可让模型给"表情 + 一句话"而非纯表情，或设 `UGREEN_ALLOW_EMOJI_REPLY=0` 只用文字回复。

**Q：想完全禁止自动发帖，只做监控？**
保持默认 `UGREEN_DRY_RUN=1` 即可，此时不需要 Cookie 和 API Key 也能跑通抓取与报告。

---

## 七、合规提醒

绿联社区《社区公约》明确禁止"同一用户多次重复发布相同内容"和"无意义或灌水帖子和评论"，违规可删帖、警告、限制发帖权限直至封禁账号。原脚本把随机语料刷到《侵权投诉指引》上，既踩了规则，观感也很差。

本工程加了黑名单、去重、内容质检和回复上限，但**自动回复本质上仍是自动化发帖**，请：

- 先用演练模式看几天输出，确认回复确实有帮助再考虑开实发；
- 保持低频（默认每天最多 5 条），不要把间隔调到 0；
- 定期检查 `ugreen_state.json` 与社区通知；
- 若社区不欢迎自动化回复，请立即停止使用。

---

## 八、目录

```
ugreen-ai-reply/
├── ugreen_ai_reply.py     # 主脚本（单文件，青龙直接 task 运行）
├── requirements.txt
├── config.example.env     # 环境变量样例
├── tests/test_core.py     # 纯函数离线单测
└── README.md
```

运行后会额外生成 `ugreen_state.json`（已回复记录、最近回复文本、上次水位），已加入 `.gitignore`。

离线测试：

```bash
python -m unittest discover -s tests -v
```
