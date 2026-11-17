# ugreen-club-ai-reply

绿联 NAS 社区自动回复脚本的重写版：**默认从论坛导读抓帖**（不区分板块、全站按发布时间倒序，`UGREEN_USE_GUIDE=0` 可改回按板块 RSS），把首楼正文、**已有楼层回复**和**检索到的绿联官方知识库资料**一起交给大模型，先判断值不值得回、有没有可靠依据，再以真实用户的口吻回复；没有可补充的技术点时，也允许只回一个表情。适用于青龙面板定时任务，也可本地直接跑。

v1.1.0 起增加三层保障：**A** 读已有楼层（避免重复回答、跳过已解决帖）、**B** 保守模式（无证据不得下确定性结论或报版本号）、**D** 可插拔检索（默认查 [绿联官方知识库](https://support.ugnas.com/knowledgecenter/)，可叠加本地知识库 / Tavily / Bing / SearXNG / 站内搜索）。

源码按功能拆分在 `src/ugreen/` 包里（见文末「工程结构」）；仓库根目录的 `ugreen_ai_reply.py` 是自动生成的自包含单文件，**青龙用户只用管它一个文件**，用法与拆分前完全一致。

---

## 一、先说清楚：默认是演练模式

**默认 `UGREEN_DRY_RUN=1`，只抓取、只生成、只打印，绝不发帖。**

确认报告和回复内容都满意后，再设置 `UGREEN_DRY_RUN=0` 才会真正发送。自动在公共社区发内容是有后果的（见文末"合规提醒"），这个默认值是故意的。

---

## 二、修了原项目的哪些问题

| 问题 | 原脚本 | 本工程 |
| --- | --- | --- |
| 抓帖子 | `a[href*="thread-"]`，只命中页脚 3 个伪静态链接（tid 29 社区公约 / 30 隐私政策 / 104 侵权投诉指引），一页 80+ 个真实帖子全漏 | 默认抓**论坛导读**（`mod=guide&view=newthread`，不区分板块、按发布时间倒序，实测覆盖 9 个板块）；也支持各板块 RSS（`mod=rss&fid=x&auth=0`，pubDate 精确到秒）与列表页兜底（限定在 `#threadlisttableid` 容器内，避开 `#layer_bbrules` 隐藏层和页脚） |
| 板块 | 3 个（fid 10/9/13） | 导读模式：**全站不区分板块**，覆盖功能讨论 / 玩法攻略 / 用户互助 / Docker / 社区活动 / 新手入门 / 需求广场 / 开发者平台 / 硬件讨论等 9 个；板块模式可指定 6 个 |
| 选帖 | 全部候选随机抽 20 次，容易反复打在同一帖 | 导读模式取**全站最新 N 篇**（默认 20）；板块模式**每个板块各取最新 N 篇**（默认 4）再交错。再按时间窗口、黑名单、已回复记录过滤 |
| 回复内容 | 36 条固定语料随机抽，与帖子内容无关 | 大模型读标题 + 首楼正文后生成，并先判断 `should_reply`；纯签到/灌水/公告帖直接跳过 |
| 回复形态 | 只能发固定长句 | 文字回复（`text`，30~120 字）或**表情回复**（`emoji`，纯表情 / 表情加 6 字以内短语）；表情同样过质检，套话、外链、AI 自述、连续刷同一表情都会被拦 |
| 成功判定 | `status_code == 200` 就算成功（Discuz 拒绝回复也是 200） | 解析响应体，先匹配失败标记再匹配成功标记，都不命中一律按失败处理；标记可用环境变量覆盖 |
| 去重 | 无，20 次 `random.choice` 会反复刷同一帖 | 状态文件记录已回复 tid；官方制度帖 tid 与标题黑名单；每次回复条数上限；**每天总条数上限**；随机间隔 |
| 回复地址 | 手工拼 `mod=post&action=reply` URL，缺 fid/extra | 直接取详情页 `form#fastpostform` 的 action；formhash 取 `input[name=formhash]` |
| 企业微信推送 | 把 `QYWX_AM`（corpid 四段）当成 PushPlus 的 token 拼进 `qyapi.pushplus.plus`，必然失败 | 依次走青龙 `sendNotify` → `QYWX_KEY` 群机器人 → `QYWX_AM` 应用消息（按青龙标准格式 `corpid,corpsecret,touser,agentid`，并兼容写反的顺序） |
| 统计 | 关键词命中后风格变量被永久改成"技术型"，汇总失真 | 按帖逐条记录 AI 判断 / 质检 / 发送结果 |
| 耗时 | 20 条 × 30~600 秒，最长 3 小时以上 | 默认每次最多 5 条、每天最多 10 条，间隔 60~180 秒，详情抓取上限 30 篇 |
| 上下文 | 只看标题，看不到别人已回复什么 | 抓前 8 条已有楼层（作者 / 楼层 / 时间 / 正文）；楼主已表示解决则直接跳过 |
| 重复回复 | 无去重，同一帖可能反复回 | 状态文件记录已回复 tid，**默认永不再回**；只有被人点名或出现更优解法才重访，且新回复与上次雷同会被拦下 |
| 事实可靠性 | 固定语料，完全不查证 | 默认检索绿联官方知识库，并把检索片段作为 `evidence` 注入提示词；无证据时禁止下确定性结论、禁止报具体版本号，本地质检会硬拦；知识库不够时自动用已配的联网源（Tavily / 百度 / Bing / SearXNG）补查 |
| 内容安全 | 无 | 辱骂、人身攻击、色情赌博、政治敏感等话题由本地正则硬闸拦截，发不出去；图文只能走白名单代码，模型无法塞任意图片链接 |
| 水贴 | 只能发固定长句 | 单独的水贴形态（2~20 字 / 表情包），每次运行限量，不会当成技术咨询 |

---

## 三、快速开始

### 本地

```bash
cd ugreen-club-ai-reply
pip install -r requirements.txt

# 演练：不需要 Cookie、不需要 API Key，会抓真实帖子并用本地模板演示
python ugreen_ai_reply.py --hours 24

# 接入大模型再看效果（仍然不发帖）
export AI_API_KEY=sk-xxx
python ugreen_ai_reply.py --hours 24

# 先看生效配置（Key 脱敏），再单独测一次模型连通性
python ugreen_ai_reply.py --show-config
python ugreen_ai_reply.py --test-ai

# 只测知识库检索：不访问社区、不调模型、不发帖，直接打印证据
python ugreen_ai_reply.py --test-search "硬盘休眠"
python ugreen_ai_reply.py --test-search "Docker 端口映射"

# 只看某几个板块，每个板块取最新 2 篇，最多拟回复 3 条
# 注意：--fids / --per-forum 需要配合 --forums（关掉导读模式）才生效
python ugreen_ai_reply.py --forums --fids 14,10 --hours 48 --per-forum 2 --limit 3

# 导读模式下单独调候选数量 / 页数
python ugreen_ai_reply.py --use-guide --guide-limit 12 --guide-pages 2
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
   - 这个文件是**自包含的单文件产物**（由 `ugreen/` 包自动生成），不需要连带 `ugreen/` 目录。
   - 想用整仓库开发，把仓库整个拉下来后跑 `python -m ugreen` 也一样。
   - **订阅链接**（青龙 → 订阅管理 → 新建，填这个 raw 地址）：

     ```
     https://raw.githubusercontent.com/thinkcodee/ugreen-club-ai-reply/main/ugreen_ai_reply.py
     ```

     仓库若再改名，raw 地址也要跟着换（`github.com` 那层会重定向，raw 这层不会）。
2. 依赖管理 → Python3 添加 `requests`、`beautifulsoup4`（`lxml` 可选，装上解析更快）；脚本缺依赖时也会尝试自动装一次。
3. 环境变量见下表，建议至少配 `UGREEN_COOKIE`、`AI_API_KEY`、`UGREEN_DRY_RUN`。
4. 定时任务：`task ugreen_ai_reply.py`，建议 `0 9,20 * * *` 之类低频规则；无论任务跑几次，一天累计也不会超过 `UGREEN_DAILY_QUOTA`（默认 10 条）。
5. 通知：青龙已配好企业微信时无需额外设置，脚本会自动调用 `sendNotify`；也可单独配 `QYWX_KEY` 或 `QYWX_AM`。

---

## 四、环境变量

### 抓取与发送

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `UGREEN_COOKIE` | 空 | 登录后的完整 Cookie；**实发模式必填** |
| `UGREEN_DRY_RUN` | `1` | `0` 才真正发帖 |
| `UGREEN_USE_GUIDE` | `1` | **帖子来源**：`1` = 论坛导读（不区分板块，按发布时间倒序）；`0` = 按下面的板块 RSS 抓。命令行 `--use-guide` / `--forums` 可覆盖 |
| `UGREEN_GUIDE_PAGES` | `1` | 导读抓几页（每页 50 条） |
| `UGREEN_GUIDE_LIMIT` | `20` | 导读模式下最多取最新多少篇进 AI 判断 |
| `UGREEN_FORUM_FIDS` | `10,9,13,14,83,12` | 板块 fid，逗号分隔。**仅在 `UGREEN_USE_GUIDE=0` 时生效** |
| `UGREEN_HOURS` | `24` | 只看最近 N 小时的新帖；实发模式下会优先沿用状态文件里的上次水位。命令行 `--hours` 可覆盖 |
| `UGREEN_PER_FORUM_LIMIT` | `4` | **每个板块各取最新几篇**（仅板块模式）；六个板块按轮转交错，谁都不会被挤掉。命令行 `--per-forum` 可覆盖 |
| `UGREEN_ALLOW_EMOJI_REPLY` | `1` | 允许表情回复；`0` 或 `--no-emoji` 只发文字回复 |
| `UGREEN_MAX_DETAIL` | `30` | 最多抓多少篇详情页（正文 + formhash），需不小于板块数 × 每板块篇数 |
| `UGREEN_MAX_REPLIES` | `5` | 每次运行最多发几条（含表情回复），是上限不是固定值。命令行 `--limit` 可覆盖 |
| `UGREEN_DAILY_QUOTA` | `10` | **每天最多发几条**（跨运行累计，按本地自然日）。`0` = 不限制。命令行 `--daily-quota` 可覆盖 |
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
| `AI_MAX_TOKENS` | `131072` | 单次最大输出 token（128K）。**设 `0` = 不限制**（请求体里不带该字段，由服务端按模型上限决定）；服务端嫌太大返回 400 时会自动去掉该字段并重试 |
| `AI_MAX_TOKENS_CAP` | `1048576` | 输出被截断时自适应放宽的天花板（1M）；设 `0` = 不设顶 |
| `AI_TOP_P` | 不传 | 设了才加入请求体 |
| `AI_BATCH_SIZE` | `5` | 每批送几篇给模型 |
| `AI_RETRIES` | `3` | 失败/JSON 解析失败重试次数（1~6） |
| `AI_JSON_MODE` | `1` | 请求 `response_format=json_object`；端点返回 400 不支持时自动去掉并重试 |
| `AI_BATCH_FALLBACK` | `1` | 整批解析失败时对缺失的帖子逐篇重试，避免一批帖子被静默丢掉 |
| `AI_DISABLE_THINKING` | 自动 | 设 `1`/`0` 强制开关；**不设时按模型名自动判断**：Xing / Qwen3 / R1 等思考型模型默认带上 `enable_thinking=false`，避免 token 全被思考过程吃掉 |
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

### 上下文与事实约束（A / B）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `UGREEN_FETCH_REPLIES` | `1` | 抓取详情页里已有的楼层回复；`0` 或 `--no-replies` 关闭 |
| `UGREEN_MAX_FLOORS` | `8` | 每篇最多取几楼（不含首楼） |
| `UGREEN_FLOOR_CHARS` | `300` | 每条楼层正文截断字数；全部楼层合计上限 1200 字 |
| `UGREEN_SKIP_SOLVED` | `1` | 楼主在楼里说「已解决 / 搞定了」时直接跳过该帖 |
| `UGREEN_CONSERVATIVE` | `1` | **保守模式**：本地硬闸拦截无证据的确定性结论、具体版本号 / 固件号 / 参数、伪造的证据编号；`0` 或 `--no-conservative` 关闭（不建议） |
| `UGREEN_REQUIRE_EVIDENCE` | `1` | 模型想下结论（`answer_mode=direct`）时必须引用至少一条真实存在的证据编号 |

### 回复策略：重访 / 水帖 / 学习点赞 / 图文

对应「回复过的帖子尽量别再回」「水贴可以简单回」「分享帖自荐帖可以回学习点赞」「可以适当图文」这四条。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `UGREEN_RECHECK_REPLIED` | `1` | 是否允许回头看已回复过的帖子；`0` 则永久不再回 |
| `UGREEN_RECHECK_LIMIT` | `3` | 每次运行最多回头看几篇 |
| `UGREEN_REVISIT_HOURS` | `168` | 只回溯最近多少小时（默认 7 天）内回复过的帖子 |
| `UGREEN_MY_USERNAME` | 空 | 你的社区用户名。别人 @ 你或提到你时会触发重访；留空则只靠「更优解」触发 |
| `UGREEN_ALLOW_CASUAL_REPLY` | `1` | 水贴（签到 / 闲聊 / 打卡）允许用一句简短回应或表情包 |
| `UGREEN_MAX_CASUAL` | `1` | 每次运行最多几条水帖回复，防止刷屏 |
| `UGREEN_ALLOW_LEARN_REPLY` | `1` | 分享帖 / 自荐帖 / 教程帖允许回「学习、点赞」型回应 |
| `UGREEN_MAX_LEARN` | `3` | 每次运行最多几条学习点赞型回复，防止刷屏 |
| `UGREEN_ALLOW_MEDIA` | `1` | 是否允许附带论坛表情 / 图片代码 |
| `UGREEN_MEDIA_CODES` | `[赞],[图片],[示意图],[加油],[鼓掌],[哈哈],[谢谢],[握手]` | **白名单**：模型只能填这里面的代码，其它一律丢弃，绝不允许自由塞图片链接 |
| `UGREEN_LOG_TITLE_CHARS` | `0` | 日志里「最新：xxx」那行的标题显示长度；`0` = 完整显示，截断时带 `…` |

**重访的判定逻辑**：已回复过的帖子默认不再回复。只有命中下面任一触发条件才会重新进入模型判断，并且新回复若与上次内容高度相似会被本地硬闸拦掉：

1. 有人点名你 —— 楼层里出现 `@你的用户名` 或你的用户名（需配置 `UGREEN_MY_USERNAME`）；
2. 出现了更优解法 —— 新楼层里出现「其实可以 / 更好的办法 / 亲测可用 / 根本原因是 / 官方给出了」这类措辞。

重访只回溯 `UGREEN_REVISIT_HOURS` 窗口内、且上次回复在最近 `UGREEN_RECHECK_LIMIT` 篇内的帖子，避免每次都翻很长的历史。

**分享帖 / 自荐帖 / 教程帖 → 回学习、点赞**：

这类帖子作者是在分享自己的东西（`【自荐】`、`【uglog】`、`实战`、`教程`、`手把手`…），不是在提问，**没有"答案"可给**。以前模型常以「楼主已自行说明用途」「教程很完整，没有可补充的点」为由跳过 —— 现在这类帖子会回一句 `kind="learn"` 的学习 / 点赞回应：

```
「WorkBuddy」看着不错，回头试试 👍
「uglog」这个思路不错，学到了 👍
赞
学到了
```

两条防线保证它既不漏、也不刷屏：

1. **不漏**：模型跳过时脚本会兜底补一条 —— 但只在标题判定为分享帖、且跳过理由是**软理由**时才补；理由里含「广告 / 重复 / 官方 / 违规 / 色情」等硬理由则尊重跳过。
2. **不刷屏**：每次运行最多 `UGREEN_MAX_LEARN` 条（默认 3）；楼层里已经有人回过「感谢分享 / 学到了 / 赞一个」这类话就不再重复；回复与近期已发内容高度相似也会被拦。

**固定语料的放行条件**变了：原脚本的固定语料（`感谢分享`、`支持楼主`、`学习了`、`赞一个`、`太棒了`…）原先一律当灌水拦死，现在 ——

* `kind="learn"`（学习点赞型）里**放行**，因为这些词本身就是这类回应的正当内容；
* 分享帖 / 自荐帖 / 教程帖里**放行**（哪怕模型用了 `kind="text"`）；
* **技术性回复里仍然拦** —— 那是答案，掺一句套话才是灌水。

### 检索（D）

默认 **`UGREEN_SEARCH_PROVIDERS=ugnas-kb,tavily,baidu`**：绿联官方知识库零配置可用，另外两个联网源**没配 Key 时直接返回空、不会发出任何请求**；知识库查到足够证据就不联网，证据不足（少于 `UGREEN_WEB_FALLBACK_THRESHOLD` 条）才自动联网补查。可用源：`ugnas-kb`、`local`、`site`、`tavily`、`bing`、`baidu`、`searxng`、`custom`，逗号分隔；填 `none` 或设 `UGREEN_SEARCH=0` / `--no-search` 关闭。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `UGREEN_SEARCH` | `1` | 总开关 |
| `UGREEN_SEARCH_PROVIDERS` | `ugnas-kb,tavily,baidu` | 检索源与顺序；命令行 `--search-providers` 可覆盖 |
| `UGREEN_SEARCH_MAX_QUERIES` | `8` | 每次运行最多给几篇帖子做检索（按候选顺序，超出的不查） |
| `UGREEN_SEARCH_MAX_EVIDENCE` | `4` | 每篇最多保留几条证据 |
| `UGREEN_SEARCH_SNIPPET_CHARS` | `400` | 单条证据片段字数 |
| `UGREEN_SEARCH_TOTAL_CHARS` | `1800` | 每篇证据总字数预算，控制 prompt 体积与成本 |
| `UGREEN_SEARCH_MIN_RELEVANCE` | `0.12` | 只丢明显跑题的证据；其余带 `relevance` 分数交给模型判断 |
| `UGREEN_SEARCH_LANGUAGE` | `zh-CN` | 知识库语言，可选 `zh-TW` / `en-US` / `de-DE` / `ja-JP` 等 |
| `UGREEN_SEARCH_DELAY` | `0.8` | 检索请求间隔（秒） |
| `UGREEN_SEARCH_TIMEOUT` | `20` | 检索请求超时（秒） |
| `UGREEN_KB_DETAIL` | `1` | 对排在前面的文章再取一次完整正文（比搜索摘要更实） |
| `UGREEN_KB_DETAIL_LIMIT` | `1` | 每次检索取几篇全文 |
| `UGREEN_WEB_FALLBACK_THRESHOLD` | `1` | 一手资料少于几条时触发联网补查 |
| `UGREEN_LOCAL_KB_DIR` | `./knowledge` | 本地知识库目录，放 `.md` / `.txt`，按 Markdown 标题分块做词频检索 |
| `TAVILY_API_KEY` | 空 | 启用 Tavily 联网检索 |
| `BING_SEARCH_KEY` / `BING_API_KEY` | 空 | 启用 Bing Web Search v7 |
| `BAIDU_SEARCH_KEY` | 空 | 启用百度检索（也可用 `QIANFAN_API_KEY` / `APPBUILDER_TOKEN` / `BAIDU_API_KEY`） |
| `SEARXNG_URL` | 空 | 自建 SearXNG 地址，如 `http://127.0.0.1:8080` |
| `UGREEN_SEARCH_CUSTOM_URL` | 空 | 自定义 GET 端点，URL 里可放 `{query}`，否则追加 `?q=` |
| `UGREEN_SEARCH_RESULTS_PATH` | `results` | 自定义端点里结果数组的点分路径，如 `data.items` |
| `UGREEN_SEARCH_TITLE_FIELD` / `_SNIPPET_FIELD` / `_URL_FIELD` | `title` / `content` / `url` | 结果条目字段名 |
| `UGREEN_SEARCH_CUSTOM_HEADERS` | 空 | JSON 对象或 `K: V; K2: V2` |

**百度检索**默认走千帆 AI 搜索端点 `https://qianfan.baidubce.com/v2/ai_search/web_search`，返回结构留空时自动探测 `references` / `results` / `data` 等常见路径；端点、鉴权头、结果路径都可改：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BAIDU_SEARCH_URL` | 千帆 `.../v2/ai_search/web_search` | 检索端点 |
| `BAIDU_SEARCH_ENGINE` | `baidu_search` | 搜索引擎标识 |
| `BAIDU_SEARCH_TOP_K` | `5` | 每次取几条结果 |
| `BAIDU_SEARCH_RESULTS_PATH` | 空 | 结果数组的点分路径，如 `data.references`；留空自动探测 |
| `BAIDU_AUTH_HEADER` / `BAIDU_AUTH_SCHEME` | `Authorization` / `Bearer` | 鉴权头与前缀，scheme 留空则直接放 Key |
| `BAIDU_SEARCH_HEADERS` | 空 | 额外请求头，JSON 对象或 `K: V` |

关于 `site`（Discuz 站内搜索）：实测**游客态会被论坛拒绝**（返回「提示信息」页），只有配置了有效 `UGREEN_COOKIE` 时才可能拿到结果；失败会自动降级为空，不影响其它源。

### 通知

| 变量 | 格式 |
| --- | --- |
| `QYWX_KEY` | 企业微信群机器人 key |
| `QYWX_AM` | `corpid,corpsecret,touser,agentid`（青龙标准格式，可含第 5 段；脚本兼容 agentid/touser 写反的情况） |

---

## 五、一次运行的流程

```
论坛导读（默认，不区分板块 / 倒序） 或 六个板块各自 RSS
   │  ──> 按 tid 去重 ──> 时间窗口 ──> 黑名单/已回复过滤
   │
   ├─ 导读模式：全站取最新 N 篇（默认 20）
   │  板块模式：每个板块各取最新 N 篇，按板块轮转交错（默认 6 × 4 = 24 篇）
   │
   ├─ 详情页：首楼正文、标题、formhash、form#fastpostform 的 action
   │   └─ [A] 已有楼层（作者/楼层/时间/正文）；楼主已说「已解决」→ 跳过
   │
   ├─ [重访] 已回复过的帖子默认不再回；只回溯最近若干小时内回过的，
   │         抓到楼层后判断「有人点名我」或「出现更优解法」才重新送进模型
   │
   ├─ [D] 提取关键词 → 检索证据（默认绿联官方知识库，不够再联网补查）
   │   └─ 去重 / 相关性粗筛 / 字数预算裁剪 → 注入每篇的 evidence
   │
   ├─ 模型：标题 + 首楼 + 已有楼层 + evidence → should_reply / kind /
   │        answer_mode / confidence / evidence_used / format / media / reply
   │
   ├─ 内容质检：文字 8~200 字；表情 ≤20 字且含表情；水帖 2~20 字；
   │           学习点赞 1~60 字（分享帖/自荐帖/教程帖专用，放行固定语料）；
   │           全部拦套话、外链联系方式、AI 自述、辱骂、不当内容、连续重复
   │
   ├─ [B] 事实质检：无证据却报版本号 / 下确定性结论 → 拦截；
   │               声称有依据却没引用真实证据编号 → 拦截；
   │               重访帖的新回复与上次几乎一样 → 拦截
   │
   └─ 发帖（或演练）──> 解析响应体判定成败 ──> 成功才写入状态 ──> 汇总 + 推送
```

### 每次会回复多少条

日志里的 `回复上限=5` 是**全局天花板，不是每次固定发 5 条**。数量按下面几层漏斗逐层收窄（括号里是一次实测值）：

| 阶段 | 数量 | 决定因素 |
| --- | --- | --- |
| 抓帖（导读模式） | 50 篇 | 导读每页固定 50 条；板块模式则是 6 × 20 = 120 篇 |
| 时间窗口 + 黑名单 + 已回复 | 28 篇 | `UGREEN_HOURS`（默认 24）、官方制度帖黑名单、状态文件里的已回复记录 |
| 取最新 N 篇 | 20 篇 | 导读模式 `UGREEN_GUIDE_LIMIT`（默认 20）；板块模式 `UGREEN_PER_FORUM_LIMIT` 每板块各取并交错 |
| AI 判断 + 内容质检 | 0~20 篇 | `should_reply=false` 跳过签到/灌水/公告；套话、外链联系方式、AI 自述、重复回复再被质检拦下 |
| **实际发送** | **0~5 条** | `UGREEN_MAX_REPLIES`（默认 5），文字与表情回复都计入；**只有发送成功才计数**，失败会继续试后面的帖子 |

即 **实际条数 = min(单次上限, 今日剩余配额, 通过 AI 判断与质检的帖子数)**，社区没新帖时为 0 条，按当前活跃度常见 1~3 条。注意候选配额（`UGREEN_GUIDE_LIMIT` / `UGREEN_PER_FORUM_LIMIT`）只是**送给 AI 判断的候选数**，**不是回复配额**——某篇帖子被判不值得回，它就是 0 条。

想调数量：

| 变量 / 参数 | 效果 |
| --- | --- |
| `UGREEN_MAX_REPLIES=3` 或 `--limit 3` | 每次最多 3 条 |
| `UGREEN_DAILY_QUOTA=5` 或 `--daily-quota 5` | 每天最多 5 条（跨运行累计） |
| `UGREEN_DAILY_QUOTA=0` 或 `--daily-quota 0` | 取消每日上限，只受单次上限约束 |
| `UGREEN_GUIDE_LIMIT=12` 或 `--guide-limit 12` | 导读模式候选最多 12 篇 |
| `UGREEN_PER_FORUM_LIMIT=2` 或 `--per-forum 2` | 板块模式每板块候选最多 2 篇 |
| `UGREEN_HOURS=48` 或 `--hours 48` | 放宽时间窗口，候选变多，才有机会接近上限 |
| `UGREEN_SKIP_EVENTS=1` | 跳过 fid=83 社区活动帖，省掉大概率被判无效的候选 |

实发时每两条回复之间随机等 `UGREEN_MIN_DELAY`~`UGREEN_MAX_DELAY`（默认 60~180 秒，最后一条后不等待），5 条大约 4~12 分钟，青龙任务的超时设置要留够；演练模式不做这个等待，所以很快。

#### 每日配额（`UGREEN_DAILY_QUOTA`，默认 10）

单次上限管的是「一次运行」，但青龙一天可能跑好几次，所以另有一道**跨运行的每日上限**：

- **默认每天最多 10 条**，按服务器本地自然日切换（`state["daily"]["YYYY-MM-DD"]` 记账）；
- **只有实发成功才计数**——失败的请求不占配额；
- **用满后整轮跳过**：不再抓帖、不再调模型、不再发帖，只推一条「今日配额已满」的提示；
- 今日还没用满时，本次可发条数 = `min(UGREEN_MAX_REPLIES, 今日剩余配额)`，比如已用 8 条、配额 10 条、单次上限 5 → 本次只发 2 条；
- 每发成功一条就立刻落盘，中途被青龙超时杀掉也不丢计数；
- **演练模式既不消耗配额，也不受配额限制**（演练不发帖）；
- 设 `UGREEN_DAILY_QUOTA=0` 即取消每日上限；配额依赖状态文件，`--no-mark` 时不生效（实发模式会给出提示）。

### 帖子来源：论坛导读（默认） vs 按板块抓

脚本支持两种抓帖方式，由 `UGREEN_USE_GUIDE` 切换，**默认走论坛导读**。

#### 模式一：论坛导读（默认，`UGREEN_USE_GUIDE=1`）

抓 <https://club.ugnas.com/forum.php?mod=guide>（"最新发表"视图）。这个页面的特点是：

- **不区分板块** —— 一页 50 条横跨全站所有板块，实测覆盖到 9 个板块，比固定 6 个板块更全（会带上"需求广场""开发者平台""硬件讨论"这些）；
- **按发布时间倒序** —— 天然就是全站最新的帖子，不用自己合并排序；
- 抓到的条目自带**板块名 + fid、作者、时间**（时间格式是"27 分钟前""昨天 23:54"这种相对值 + title 属性里的绝对日期，脚本两种都能解析）。

取多少条由 `UGREEN_GUIDE_LIMIT` 控制（默认 20），抓几页由 `UGREEN_GUIDE_PAGES` 控制（默认 1，每页 50 条）。

```
[15:16:28] 模式=演练，来源=论坛导读（不区分板块，发布时间倒序，≤20 篇 / 1 页），回复上限=5
[15:16:29]   导读 第 1 页 50 篇（累计 50 篇 / 每页 50 条）
[15:16:30] 导读汇总 50 篇（去重后）
[15:16:30] 窗口内候选 28 篇（过滤掉旧帖 / 制度帖 / 已回复）
[15:16:30]   最新：建议媒体库列表增加用户隔离功能
[15:16:30] 按导读取最新 20 篇，共 20 篇
```

导读页没有正文摘要，正文靠后续抓详情页补齐（和板块模式一样），所以详情抓取耗时不变。

#### 模式二：按板块 RSS（`UGREEN_USE_GUIDE=0`）

回到原来的行为：只抓 `UGREEN_FORUM_FIDS` 里配的那几个板块，每个板块各取最新 `UGREEN_PER_FORUM_LIMIT` 篇再交错送进 AI 判断。

适合这两种情况：

- 只想覆盖自己关心的几个板块，不想被"需求广场"这类板块刷屏；
- 导读页抓不到（站点改版）时的退路。

```
[15:17:36] 模式=演练，来源=板块 RSS，板块=[10, 9, 13, 14, 83, 12]，每板块最新 4 篇
[15:17:43]   板块 玩法攻略 最新 2/2 篇，最新：【自荐】给绿联NAS请了一个AI管家，从此动动嘴就能玩转NAS
```

#### 怎么切

```bash
# 强制用导读
python ugreen_ai_reply.py --use-guide
# 强制按板块
python ugreen_ai_reply.py --forums
# 环境变量（青龙里更常用）
UGREEN_USE_GUIDE=0
```

两种模式共用同一套 AI 判断、检索、质检逻辑，切换只影响"从哪儿拿帖子"。

### 每个板块取最新，而不是全局取最新

> 仅**板块模式**适用。跨板块合并后如果直接按时间截断，结果会变成"哪个板块更新得快就全是它的"。所以脚本按 `fid` 分组，每板块各取最新 `UGREEN_PER_FORUM_LIMIT` 篇，再按板块轮转交错送进 AI 判断。日志会逐行打印每个板块取了几篇、最新一篇是什么，方便核对；`UGREEN_MAX_DETAIL` 要留够 `板块数 × 每板块篇数`，默认 30 足够。
>
> **导读模式本身就是全局时间倒序**，不再做分板块配额，直接取最新 `UGREEN_GUIDE_LIMIT` 篇。

### 挑选"有意义"的回复 + 表情回复

模型输出严格 JSON，每篇帖子一条：

```json
{"results":[
  {"tid":"7342","should_reply":true,"kind":"text",
   "answer_mode":"probe","confidence":"low","evidence_used":[],
   "reply":"Vivo 的桌面快捷方式是系统权限控制的，可以先确认一下有没有给「创建桌面快捷方式」权限，这个我不太确定。",
   "reason":"具体机型，但缺环境信息"},
  {"tid":"7332","should_reply":true,"kind":"text",
   "answer_mode":"direct","confidence":"high","evidence_used":["kb:876"],
   "reply":"官方文档里提到硬盘休眠要求一段时间没有读写任务，可以先看看是不是 Docker 虚拟机或下载任务在后台跑。",
   "reason":"官方知识库有直接依据"},
  {"tid":"7323","should_reply":true,"kind":"emoji",
   "answer_mode":"emoji","confidence":"low","evidence_used":[],
   "reply":"👍","reason":"教程很完整，无可补充"},
  {"tid":"7311","should_reply":true,"kind":"casual",
   "answer_mode":"casual","confidence":"low","evidence_used":[],
   "media":"[赞]","reply":"这个我也有同感",
   "reason":"水帖，简单回应"},
  {"tid":"7327","should_reply":true,"kind":"learn",
   "answer_mode":"learn","confidence":"low","evidence_used":[],
   "reply":"「uglog」这个思路不错，学到了 👍",
   "reason":"自荐帖，回个学习点赞"},
  {"tid":"7339","should_reply":false,"kind":"text","reply":"",
   "reason":"纯反馈无信息量"}
]}
```

系统提示词明确要求：文字回复必须紧扣帖子的具体内容、不编造功能与版本、不假装官方、不留外链联系方式、30~160 字；技术性回复不写"感谢分享/支持楼主"这类套话；信息不足时只追问一个关键信息。不值得回的帖子直接 `should_reply=false`。

`answer_mode` 只有 `direct`（给结论）、`probe`（追问 / 排查方向）、`emoji`、`casual`、`learn` 五种；**只有 `confidence=high` 且 `evidence_used` 非空才允许 `direct`**，模型自己标错也会被本地纠正或拦下。

`kind="emoji"` 用于"帖子写得很完整、没有可补充的技术点，但确实值得鼓励"的情况，允许纯表情（`👍`）、论坛表情码（`[赞]`）或"表情 + 6 字以内短语"（"这个思路很巧 👏"）。质检会放宽长度，但**仍然拦截**灌水套话、外链、联系方式、AI 自述，以及连续三次刷同一个表情。不想用表情时设 `UGREEN_ALLOW_EMOJI_REPLY=0`。

`kind="casual"` 用于**水贴**（签到、闲聊、打卡）：2~20 字的轻松回应或表情包，不带任何技术结论。技术提问一律不许用 casual。每次运行最多 `UGREEN_MAX_CASUAL` 条（默认 1），防止刷屏。

`kind="learn"` 用于**分享帖 / 自荐帖 / 教程帖**（payload 里 `is_share=true`）：一句学习、点赞式回应，2~60 字，如「赞」「学到了」「nice」「感谢分享」「这个思路不错，学到了 👍」。模型被要求**贴着帖子实际内容说**（提到具体的工具名、做法、亮点），但**不得下技术结论、不得提版本号**；`confidence` 固定 `low`、`evidence_used` 为空。固定语料（感谢分享 / 赞一个 / 学习了…）在这个形态下合法，在技术性回复里仍会被拦。每次运行最多 `UGREEN_MAX_LEARN` 条（默认 3）。

`format="steps"` 时模型会用 `1. … 2. … 3. …` 或换行分点写多步操作，报告里会标「分步」；单段说明用 `"plain"`。

`media` 是**可选的论坛表情 / 图片代码**，且必须落在 `UGREEN_MEDIA_CODES` 白名单里（默认 `[赞] [图片] [示意图] [加油] [鼓掌] [哈哈] [谢谢] [握手]`）。白名单之外的代码会被直接丢弃，**模型无法自由塞任何图片链接**。每条回复最多带一个。

### A / B / D：楼层、证据与事实约束

**A · 已有楼层**：详情页里前 8 楼的作者、楼层号、精确时间和正文会一起送进 prompt。模型被要求不要复读别人已给出的正确答案；若楼主在楼里说了「已解决 / 搞定了 / 可以了」，该帖直接跳过（可用 `UGREEN_SKIP_SOLVED=0` 关闭）。实测 11 篇候选共抓到 29 条楼层。

> 绿联用的是 comiis 皮肤，用户名在 `a.km_name`（`display:none`）里，而同一个 `div.authi` 下第一个 `a[href*=mod=space]` 是**没有文本的头像链接**——取错会让作者名变空，"楼主是否已解决"就永远判断不出来。脚本已按 `km_name → xw1 → mod=space` 的顺序取并跳过空文本，同时从首楼回填楼主名。

**重访 · 已回复过的帖**：默认**不再回复**。只有命中触发条件才会重新送进模型：① 有人点名你（需配 `UGREEN_MY_USERNAME`）；② 新楼层出现更优解法。新回复若与上次内容相似度超过 0.85 会被本地硬闸拦掉。回溯窗口与条数由 `UGREEN_REVISIT_HOURS` / `UGREEN_RECHECK_LIMIT` 控制。

**D · 检索**：默认查绿联官方知识库 `https://support.ugnas.com/knowledgecenter/` 网页端同源的公开只读接口（`api.ugnas.com/.../base/search`），返回标题、摘要、正文片段，必要时再取一次文章全文。证据带 `ref`（如 `kb:876`）、`source` 和 `relevance`。单次运行最多给 8 篇帖子检索，每篇最多 4 条、总字数 1800 字，并有请求间隔和失败降级。想离线复现或补充自有资料，把 Markdown 放进 `knowledge/` 并加 `local` 源即可；`--test-search "硬盘休眠"` 可以只跑检索看结果。

知识库查到的证据少于 `UGREEN_WEB_FALLBACK_THRESHOLD` 条时，会自动调用已配置的联网源补查（默认列表 `ugnas-kb,tavily,baidu`；Tavily / 百度**没配 Key 时直接返回空、不发任何请求**）。百度默认走千帆 AI 搜索端点，端点、鉴权头、结果路径都能改。

**B · 保守模式**：提示词要求模型只能把 `evidence` 里**明确写了**的事实作为结论，且必须引用对应 `ref`；没有证据时只追问一个关键信息或给不带结论的排查顺序。发出之前还有一道本地硬闸：

- 没有证据却出现版本号 / 固件号 / 具体参数（如 `1.17.0.0031`）→ 拦截；
- 没有证据却出现「肯定 / 官方已经支持 / 据官方文档 / 不支持 X」等确定性或官方口吻 → 拦截；
- `answer_mode=direct` 或 `confidence=high` 但根本没有检索证据 → 拦截；
- 引用了不存在的证据编号 → 拦截；
- 表情回复里夹带技术断言 → 拦截；
- 重访帖的新回复与上次几乎一样 → 拦截。

**内容安全**：辱骂、人身攻击、歧视、嘲讽，以及色情、赌博、暴力、违法破解、政治敏感等话题，除了提示词里明令禁止外，还有一道本地正则硬闸 `ABUSIVE_RE` / `UNSAFE_TOPIC_RE` 兜底——即使模型违规输出，也**发不出去**。同时照旧拦截灌水套话、外链、联系方式与 AI 自述。

**必须说清的局限**：以上约束能挡住「凭空编版本号」「发辱骂内容」这类硬伤，但**无法校验回复与证据在语义上是否一致**，也无法保证检索到的资料一定与帖子相关。官方知识库用模糊检索，可能返回沾边的文档；脚本会做字面相关性粗筛并把分数交给模型，最终判断仍依赖模型遵守提示词。它现在是「会查官方文档、且被规则约束的经验型回复」，**不是查证过的事实型回复**。建议先看几天演练输出，再决定是否开实发。

---

## 六、常见问题

**Q：日志说"RSS 汇总 0 篇"？**
先确认 `https://club.ugnas.com/forum.php?mod=rss&fid=10&auth=0` 能否打开。RSS 关闭时脚本会自动走列表页兜底（`UGREEN_USE_LIST_FALLBACK=1`）。注意这行只在**板块模式**（`UGREEN_USE_GUIDE=0`）下出现；默认的导读模式对应的是"导读汇总 N 篇"。

**Q：默认抓的帖子和之前不一样了？**
v1.1.5 起默认走**论坛导读**（`UGREEN_USE_GUIDE=1`）：不区分板块、全站按发布时间倒序，覆盖的板块比原来配的 6 个更全（实测 9 个），所以候选帖会和之前不同。想恢复原来的按板块抓，设 `UGREEN_USE_GUIDE=0` 或加 `--forums`。

**Q：导读页抓到的帖子没有正文？**
导读页本身只有标题、作者、板块、时间，没有正文摘要。正文由后续的详情页抓取补齐（和板块模式一样），所以详情抓取耗时不变、AI 拿到的上下文也一样完整。

**Q：导读页的时间是"27 分钟前"这种，能用吗？**
能。脚本同时解析文本里的相对时间（"N 分钟前 / 半小时前 / N 小时前 / N 天前"）和 `title` 属性里的绝对日期，"昨天 23:54""前天 09:14"这类钟点也能还原到分钟。

**Q：实发模式提示"Cookie 无效或已过期"？**
浏览器重新登录，`F12 → Network → 任意请求 → Request Headers → Cookie` 整串复制。Cookie 一般几十天过期，建议定期更新。

**Q：今天已经发过几条了？怎么停？**
日志开头就有 `每日配额 10 条：今日已用 8 条，剩余 2 条`。计数写在状态文件 `ugreen_state.json` 的 `daily` 字段里，只有实发成功才计入。想调上限改 `UGREEN_DAILY_QUOTA`（`0` = 不限制）；想临时停一天，把它设成 `1` 即可（已发的量不会清零，只是几乎发不了新的）。

**Q：明明发帖成功，日志却说"响应中未找到成功标记"？**
Discuz 模板不同，成功文案可能变化。设 `UGREEN_DEBUG=1` 看响应体片段，把其中的成功字样加进 `UGREEN_SUCCESS_MARKERS`。**不要**简单把这条改成"只要 200 就算成功"——那正是原脚本误报的根源。

**Q：模型连不上 / 404 / 401，怎么知道青龙里的变量有没有生效？**
先跑 `python ugreen_ai_reply.py --show-config`，它会打印最终 endpoint、model、baseURL 和 Key 来自哪个环境变量（Key 脱敏）；再跑 `--test-ai` 单独发一条最小请求。常见原因：baseURL 漏 `/v1`（脚本会自动补）、把完整 `/chat/completions` 又拼了一次（脚本会去重）、`AI_ENABLED=0`、或某个优先级更高的别名变量（如 `API_KEY`）覆盖了真正想用的 Key。带 deployment/query 的端点请直接设 `AI_ENDPOINT`。

**Q：AI 一直返回不合法 JSON？**
思考型模型（SiliconFlow 的 `XingChenAGI/Xing4.0-*`、Qwen3 等）常见两种翻车，v1.1.1 起都能自动兜住：

1. **JSON 前面带了一段说明文字**，或套了 ` ```json ` 围栏 → 现在会先剥围栏、再扫描第一组配平的 `{ }` / `[ ]` 抠出 JSON；
2. **token 全花在思考过程上，正文是空的**（`finish_reason=length`）→ 检测到这种情况会自动放宽 `max_tokens`、并在下一轮带上 `enable_thinking=false`。

失败时日志会打印 `finish_reason`、正文长度和**原始返回片段**，照着看就能定位。若你的模型支持关思考，建议直接设 `AI_DISABLE_THINKING=1`；端点不认这个参数时脚本会自动去掉并继续。整批失败后还会对缺失的帖子逐篇重试（`AI_BATCH_FALLBACK=1`），不会再整批丢掉。

老版本如果连 `response_format` 都不支持，设 `AI_JSON_MODE=0`。

**Q：`max_tokens` 到底能给多大？会不会把正文挤没？**
现在的模型动辄 128K~1M，所以：

- 默认 `AI_MAX_TOKENS=131072`（128K），`AI_MAX_TOKENS_CAP=1048576`（1M）是自适应放宽的天花板；
- **`AI_MAX_TOKENS=0` 表示完全不限制** —— 请求体里干脆不带 `max_tokens` 字段，由服务端按模型自身上限决定；
- 万一服务端嫌这个数太大返回 400，脚本会自动去掉该字段并重试（并把后续批次也切成不限制）；
- 若日志里 `finish_reason=length` 且原始片段是英文长篇分析（如 "Let me analyze each post one by one"），说明那是**思考过程**不是正文。脚本已按模型名自动关思考（Xing / Qwen3 / R1 等），你也可以用 `AI_DISABLE_THINKING=1` 强制；单纯调大 `max_tokens` 只会让它思考得更久。

**Q：日志里「最新：xxx」的标题被截断了吗？**
帖子标题本身一直是完整的，截断只发生在**日志展示层**。现在默认**完整显示**；确实嫌长可设 `UGREEN_LOG_TITLE_CHARS=40`，截断处会带 `…`，一眼能看出是显示层截断而不是标题就这么短。

**Q：表情回复被论坛拒了？**
部分 Discuz 配置会限制过短内容，响应里出现"内容太短"时脚本会如实记为失败（这比原脚本误报成功要好）。可让模型给"表情 + 一句话"而非纯表情，或设 `UGREEN_ALLOW_EMOJI_REPLY=0` 只用文字回复。

**Q：检索返回 0 条，或者回复全被"保守模式拦截"？**
先跑 `python ugreen_ai_reply.py --test-search "关键词"` 确认知识库接口本身通不通。检索失败会自动降级为空，此时保守模式下模型只能追问或给排查方向，很多帖子会被判 `should_reply=false` —— 这是**故意的安全行为**，不是故障。若确实不需要约束，可设 `UGREEN_CONSERVATIVE=0`，但会显著提高答错风险。

**Q：怎么加自己的资料？**
在脚本目录下建 `knowledge/`，放 `.md` / `.txt`（按 `#` 标题分块），再设 `UGREEN_SEARCH_PROVIDERS=ugnas-kb,local`。也可以接 Tavily / Bing / 百度 / SearXNG / 自定义 HTTP 端点，详见第四节。

**Q：我回过的帖子还会再回吗？**
默认**不会**。已回复的 tid 记在 `ugreen_state.json` 里，之后直接过滤掉。只有两种例外会重访：① 有人 @ 你或提到你（需设 `UGREEN_MY_USERNAME=你的社区用户名`）；② 新楼层里出现明显更优的解法。日志里会打印「重访命中 tid=xxxx：xxx 点名了你」或「提到了更优解法」，报告里也会带一行 `[重访]`。完全关掉重访设 `UGREEN_RECHECK_REPLIED=0`。

**Q：百度检索要怎么配？**
在百度智能云（千帆 / AppBuilder）申请 API Key 后设 `BAIDU_SEARCH_KEY`，脚本默认打千帆的 `https://qianfan.baidubce.com/v2/ai_search/web_search`；若你的接口地址或返回结构不同，用 `BAIDU_SEARCH_URL` 和 `BAIDU_SEARCH_RESULTS_PATH` 覆盖即可。没配 Key 时该源直接返回空、不发任何请求，不影响知识库。

**Q：官方知识库接口会不会被我刷爆？**
默认每次运行最多检索 8 篇、每篇最多 2 条查询、请求间隔 0.8 秒，全部是只读 GET，且结果在同一次运行内缓存。请保持低频，不要用 `UGREEN_SEARCH_MAX_QUERIES` 做批量爬取；站点 `robots.txt` 也明确禁止爬取 `/api/` 路径下的内容。

**Q：想完全禁止自动发帖，只做监控？**
保持默认 `UGREEN_DRY_RUN=1` 即可，此时不需要 Cookie 和 API Key 也能跑通抓取与报告。

---

## 七、合规提醒

绿联社区《社区公约》明确禁止"同一用户多次重复发布相同内容"和"无意义或灌水帖子和评论"，违规可删帖、警告、限制发帖权限直至封禁账号。原脚本把随机语料刷到《侵权投诉指引》上，既踩了规则，观感也很差。

本工程加了黑名单、去重、内容质检、事实约束和回复上限，但**自动回复本质上仍是自动化发帖**，且模型仍可能给出过时或不对的技术信息，请：

- 先用演练模式看几天输出，确认回复确实有帮助再考虑开实发；
- 保持低频（默认每次最多 5 条、每天最多 10 条），不要把间隔调到 0；
- 定期检查 `ugreen_state.json` 与社区通知；
- 检索只用官方公开只读接口并保持低频，不要批量爬取；
- 若社区不欢迎自动化回复，或发现回复有明显错误，请立即停止使用。

---

## 八、目录

```
ugreen-club-ai-reply/
├── src/ugreen/             # 实现包（分模块，见下方「工程结构」）
│   ├── __init__.py         # 包入口，导出公共 API
│   ├── __main__.py         # 支持 python -m ugreen
│   ├── core/               # 基础设施
│   │   ├── constants.py    #   常量与提示词
│   │   ├── models.py       #   Thread / Reply / Evidence
│   │   ├── config.py       #   配置对象 Config
│   │   └── deps.py         #   第三方依赖引导
│   ├── util/               # 通用工具
│   │   ├── envutil.py      #   环境变量与日志
│   │   ├── textutil.py     #   文本清洗与截断
│   │   ├── timeutil.py     #   时间与链接解析
│   │   └── aiutil.py       #   AI 端点与密钥解析
│   ├── forum/              # 论坛交互
│   │   ├── client.py       #   抓取与发帖
│   │   ├── classify.py     #   分享帖 / 水贴 / 标题分词
│   │   ├── state.py        #   状态持久化与每日配额
│   │   └── revisit.py      #   已回复帖的二次触达
│   ├── search/             # 资料检索
│   │   └── providers.py    #   各 provider 与证据聚合
│   ├── ai/                 # 智能决策
│   │   ├── client.py       #   模型调用与响应解析
│   │   └── quality.py      #   回复质检
│   └── runtime/            # 运行编排
│       ├── cli.py          #   命令行入口与主流程
│       ├── notify.py       #   推送渠道
│       └── report.py       #   报告渲染
├── ugreen_ai_reply.py      # 单文件产物（自动生成，青龙直接 task 运行）
├── tools/build_single.py   # 把 src/ugreen/ 打包成上面的单文件
├── pyproject.toml          # 包元数据（可 pip install -e .）
├── conftest.py             # 让测试直接导入源码树
├── requirements.txt
├── config.example.env      # 环境变量样例
├── tests/test_core.py      # 179 项行为单测
├── tests/test_package.py   # 16 项工程结构测试
├── knowledge/              # 可选：本地知识库 .md/.txt（需加 local 检索源）
└── README.md
```

运行后会额外生成 `ugreen_state.json`（已回复记录、最近回复文本、上次水位），已加入 `.gitignore`。

离线测试：

```bash
python -m unittest discover -s tests -v
```

两个诊断命令都不访问社区、不调模型、不发帖：`--show-config`（打印生效配置，Key 脱敏）与 `--test-search "关键词"`（打印检索证据）。

### 工程结构：为什么要拆成包 + src 布局

源码按功能拆进 `src/ugreen/`，每个模块只管一件事，依赖单向向下：

```
core（常量/结构/配置）→ util（通用工具）→ forum / search / ai（业务能力）→ runtime（编排）
```

`core` 与 `util` 是最底层，`runtime` 在最上层，中间三个子包互不反向依赖。这条规则由测试自动校验（`test_dependency_direction`），一旦有人写出反向依赖就会测试失败。

**`src/` 布局**是 Python 官方推荐的工程结构：可被 `import` 的包只在 `src/` 下，仓库根只放「非库文件」（脚本、测试、配置、工具）。好处是本地目录不会意外遮蔽已安装的同名包，测试测到的永远是你写的代码。`tools/`、`tests/` 留在根目录正是这个规则的一部分——它们不是库，不该进 `src/`。

但**青龙用户拿到的仍是单个 `ugreen_ai_reply.py`**——它的内容由 `tools/build_single.py` 把 `src/ugreen/` 各模块按依赖序内联生成，自包含、无相对导入，放进 `scripts` 目录就能跑，和拆分前完全一样。

| 你想做什么 | 怎么做 |
| --- | --- |
| 改逻辑 | 改 `src/ugreen/` 下对应模块，然后 `python tools/build_single.py` 重新生成单文件 |
| 只用不开发 | 直接用 `ugreen_ai_reply.py`，不用管 `src/` 目录 |
| 本地开发调试 | `pip install -e .` 后 `ugreen-club-ai-reply --dry-run`，或 `python -m ugreen --dry-run`（需先 `pip install -e .`） |
| 确认单文件没过期 | `python tools/build_single.py --check`（测试里也会自动校验，CI 也会跑） |

改动 `src/ugreen/` 后忘了重新构建，测试会直接失败（`test_single_file_is_up_to_date`），不会出现「改了包、跑的还是旧逻辑」这种坑。

### 两个依赖文件的关系

`requirements.txt` 和 `pyproject.toml` 里的依赖是**手工维护的两处**（依赖就三个模块，不值得上自动生成）：

| 文件 | 作用 | 谁用 |
| --- | --- | --- |
| `requirements.txt` | 装运行时依赖 | `pip install -r requirements.txt`；青龙容器、CI |
| `pyproject.toml` | 包元数据 + 构建配置 + 依赖声明 | `pip install -e .`；`ugreen-club-ai-reply` 命令；CI |

**只跑脚本的话，有 `requirements.txt` 就够了**，`pyproject.toml` 可以不装——它额外提供的是：可编辑安装、`ugreen-club-ai-reply` 命令行入口、以及 src 布局的打包声明。

两处依赖必须保持一致，由测试守住（`test_requirements_and_pyproject_agree`）——加了依赖只改一处会直接测试失败。

### GitHub Actions

仓库配了两个工作流：

| 工作流 | 触发 | 做什么 |
| --- | --- | --- |
| `ci.yml` | push / PR / 手动 | 在 Python 3.9 / 3.11 / 3.13 上跑 195 项测试，并校验单文件产物与源码一致。**产物过期会直接失败** |
| `build-single.yml` | 发布 Release / 手动 | 重新生成单文件、跑测试；发版时把产物上传到 Release 附件，手动触发时可选把改动提交回 `main` |

手动触发构建：仓库 **Actions → 构建单文件产物 → Run workflow**。

`ci.yml` 是「忘了构建」的最后一道防线——即使本地忘了跑 `build_single.py`，PR 也会被拦下。

