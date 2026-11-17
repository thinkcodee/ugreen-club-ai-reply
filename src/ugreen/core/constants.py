"""绿联 NAS 社区自动回复 —— 常量与提示词。"""

from __future__ import annotations

from pathlib import Path
import re

BASE_URL = "https://club.ugnas.com/"
RSS_URL = BASE_URL + "forum.php?mod=rss&fid={fid}&auth=0"
FORUM_URL = BASE_URL + "forum.php?mod=forumdisplay&fid={fid}"
THREAD_URL = BASE_URL + "forum.php?mod=viewthread&tid={tid}"
# 论坛导读：不区分板块，按发布时间倒序（view=newthread 即「最新发表」）
GUIDE_URL = BASE_URL + "forum.php?mod=guide&view=newthread&page={page}"
# 导读页每页 50 条
GUIDE_PAGE_SIZE = 50

# 已实测（2026-11）：fid 与板块名一一对应，RSS 均可用
DEFAULT_FORUMS: dict[int, str] = {
    10: "功能讨论",
    9: "玩法攻略",
    13: "用户互助",
    14: "Docker",
    83: "社区活动",
    12: "新手入门",
}

# 站规 / 制度帖：原脚本刷的就是这几个（页脚 thread-xx 伪静态链接）
BLOCK_TIDS = {"28", "29", "30", "31", "104"}
BLOCK_TITLE_KEYWORDS = (
    "社区公约", "隐私政策", "侵权投诉", "用户协议", "积分规则",
    "版规", "社区规则", "管理办法", "违规处理",
)

# 原脚本的固定语料：以前一律当作「灌水」拒绝发送。
# 现在它们是 **学习/点赞型回复（kind="learn"）的合法素材** ——
# 分享帖、自荐帖本来就没有技术问题要答，回一句「感谢分享」「赞一个」才是正常社区互动。
# 但在技术性回复（kind="text"）里仍然拦：那是答案，掺一句套话就是灌水。
CLICHE_REPLIES = (
    "感谢分享", "支持楼主", "学习了", "我来打卡", "赞一个", "希望官方采纳",
    "这个功能确实值得优化", "建议加个远程端口配置选项", "我也遇到类似情况",
    "太棒了", "这招太骚了", "我抄作业来了", "高手在民间", "大家都是技术人",
    "欢迎交流", "希望你能顺利解决",
)

# 强分享标记：标题里有这些词，基本可以确定作者是在分享/写教程而不是提问。
STRONG_SHARE_MARKERS = (
    "自荐", "分享", "实战", "教程", "手把手", "从零", "攻略", "指南", "实践",
    "亲测", "评测", "盘点", "一览", "科普", "开箱", "安利", "方案",
    "踩坑记录", "经验分享", "使用记录", "全流程", "保姆级", "详解",
)
# 强求助语气：出现这些的一定是在提问，不管标题里有没有分享字样。
STRONG_QUESTION_MARKERS = (
    "求助", "求个", "求教", "请问", "请教", "求推荐", "有没有人", "急啊",
    "？", "?",
)
# 弱求助语气：只在没有强分享标记时才拿来当提问信号。
# 「如何 / 怎么」在教程标题里极常见（「ZFS如何保护我的数据」），不能单独当提问。
WEAK_QUESTION_MARKERS = (
    "怎么", "如何", "为什么", "能不能", "可不可以", "不生效", "报错",
    "失败", "无法", "出问题", "怎么办", "不进去", "不能", "问题",
)
# 方括号里的「分类标记」（学院体验、活动、反馈…）不是分享信号
CATEGORY_TAG_WORDS = ("学院", "体验", "活动", "反馈", "公告", "问答",
                       "讨论", "提问", "求助", "教程", "经验")
# 标题里的方括号标记（【自荐】【uglog】…）也是分享/自荐的强信号
BRACKET_TAG_RE = re.compile(r"[【\[][^】\]]{2,12}[】\]]")
# 楼层里已经有人做过「学习/点赞」式回应，就不必再重复
LEARN_PRAISE_WORDS = (
    "感谢分享", "学习了", "学到了", "赞一个", "支持楼主", "收藏了", "已收藏",
    "打卡", "太棒了", "不错", "受教", "nice", "赞",
)
# 模型「跳过」的理由里，这些是硬理由（广告/重复/违规），必须尊重，不再补回复；
# 其余属于「软理由」（没什么可补充、楼主已自行说明…），分享帖仍按规则回一条。
HARD_SKIP_REASONS = (
    "广告", "重复", "官方", "制度", "违规", "敏感", "色情", "赌博", "政治",
    "违法", "已解决", "无需", "别人已", "已有回复", "同名", "转载",
)

BAD_REPLY_RE = re.compile(
    r"https?://|www\.|@[\w.-]{2,}|微信|QQ群|加我|联系我|扫码|二维码|"
    r"作为\s*AI|语言模型|我是\s*AI|官方人员|官方承诺",
    re.I,
)

SOLVED_MARKERS = (
    "已解决", "解决了", "搞定了", "已经好了", "可以了", "问题已排除",
    "按你说的改好了", "找到原因了", "多谢，好了", "谢谢，好了", "结帖",
)
# 楼层正文里常见的注册提示噪声，压进 payload 前去掉
REPLY_NOISE_RE = re.compile(
    r"^马上注册.{0,80}?立即注册\s*[×x]?\s*"
    r"|^您需要\s*登录\s*才可以.{0,80}?立即注册\s*[×x]?\s*",
)
# 所有楼层正文合计的字数预算，避免把 prompt 撑爆
REPLY_BUDGET_CHARS = 1200

# 重访已回复帖的触发条件：新增楼层里出现更优解 / 楼主或他人纠正了原答案
BETTER_SOLUTION_MARKERS = (
    "其实可以", "更好的办法", "更好的方法", "正确做法是",
    "不用这么麻烦", "亲测可用", "已验证", "实测有效", "这样更简单",
    "上面说的不对", "楼上说错了", "补充一下", "还有个办法",
    "根本原因是", "官方给出了",
)

ABUSIVE_RE = re.compile(
    r"傻[逼bB叉吊]|煞笔|沙雕|废物|蠢货|白痴|智障|脑残|残废|滚蛋|去死|"
    r"狗东西|狗腿子|贱人|婊子|妓女|操你妈|草泥马|"
    r"fuck|shit|bitch|asshole|idiot|moron|"
    r"约炮|裸聊|色情|黄片|赌博|博彩|私彩|代孕|"
    r"破解版|盗版资源|破解补丁|游戏外挂|刷单返利",
    re.I,
)
UNSAFE_TOPIC_RE = re.compile(
    r"政治敏感|颠覆国家|分裂国家|恐怖袭击|制毒|制枪|买卖枪支|自残|自杀方法",
)

# 可选「图文」：只允许白名单里的论坛表情/图片代码，模型不能自由塞外链
DEFAULT_MEDIA_CODES = ("[赞]", "[图片]", "[示意图]", "[加油]", "[鼓掌]",
                       "[哈哈]", "[谢谢]", "[握手]")

# 检索分级：一手资料优先，明显不够时才动用联网源
KB_FIRST_PROVIDERS = frozenset({"ugnas-kb", "local", "site"})
WEB_PROVIDERS = frozenset({"tavily", "bing", "baidu", "searxng", "custom"})
BAIDU_RESULT_PATHS = ("references", "data.references", "result.references",
                      "results", "data.results", "data", "search_results")

# 版本号 / 固件号 / 具体参数：没有证据时出现一律拦截
VERSION_RE = re.compile(
    r"\b\d+\.\d+(?:\.\d+){0,3}\b|v\d+\.\d+|\bUGOS\s*Pro?\s*\d|"
    r"固件\s*\d|版本\s*[:：]?\s*\d|\d+\s*(?:TB|GB|MB|端口|W)",
    re.I,
)
# 确定性断言 / 官方口吻：没有 high 证据时不许说
RISK_CLAIM_RE = re.compile(
    r"肯定|一定|必然|绝对|百分百|官方(?:已经|已|明确|确认)|已经支持|"
    r"不支持|内置了|必须从|只能(?:用|在|通过)|唯一(?:方法|原因)|"
    r"实测|亲测|确认是|就是.*?问题|无需|不可能|不会(?:有|出现)|"
    r"固件版本|系统版本|适用于所有|全系(?:列)?支持|据官方文档|"
    r"根据官方(?:文档|说明)|官方文档(?:显示|说明)",
)

# 绿联官方知识库（support.ugnas.com 网页端同源的公开只读接口，已实测 2026-11）
KB_HOME_URL = "https://support.ugnas.com/knowledgecenter/"
KB_SEARCH_URL = ("https://api.ugnas.com/api/system/knowledge_center/"
                 "sa/base/search")
KB_ARTICLE_URL = ("https://api.ugnas.com/api/system/knowledge_center/"
                  "sa/base/article/info")
KB_STRATEGY = "fog"
KB_DEFAULT_LANGUAGE = "zh-CN"

# 关键词提取用的停用词（只起降噪作用，不做分词）
SEARCH_STOPWORDS = frozenset(
    "的 了 是 在 和 与 有 我 你 他 这 那 个 们 吗 呢 吧 啊 怎么 如何 什么 "
    "为什么 可以 不能 一下 一个 请教 求助 请问 大家 谢谢 感谢 分享 帖子 "
    "问题 遇到 出现 现在 已经 还是 但是 就是 以及 关于 一些 之后 之前 "
    "绿联 社区 论坛 大佬 小白 新手 求助帖 有没有 求 急 在线等".split()
)

# 允许的「表情回复」：Unicode emoji / Discuz 表情码 [哈哈] / 常见颜文字
EMOJI_PATTERN = re.compile(
    r"[\U0001F300-\U0001FAFF\U0001F900-\U0001F9FF\U00002600-\U000027BF"
    r"\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u2764]"
    r"|\[[^\]\[]{1,8}\]"
    r"|:(?:smile|grin|lol|wink|thumb|clap|heart|joy|cool|ok|rose):"
    r"|(?<!\w)[:;=]-?[)DPp](?!\w)",
    re.I,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 仓库根目录：src/ugreen/core/constants.py -> src/ugreen/core -> src/ugreen -> src -> 仓库根
# 打包成单文件后由 tools/build_single.py 替换为脚本所在目录。
SCRIPT_DIR = Path(__file__).resolve().parents[3]

SYSTEM_PROMPT = """你是绿联 NAS 社区的一位热心老用户，熟悉 UGOS Pro、Docker 容器、硬盘与存储、网络与远程访问、影音与备份等常见玩法。

你的任务：逐篇阅读帖子，判断是否值得回复，并写出一条真正有帮助、且不会误导别人的回复。

【你会收到的字段】
每篇帖子包含：tid、title、forum、author、published、content_preview（首楼正文前 800 字）、
existing_replies（该帖已有的楼层回复，含楼层号/作者/正文）、likely_solved（楼主是否已在楼里表示问题解决）、
evidence（为你检索到的资料片段，每条带 ref 编号、title、snippet、source、relevance）、keywords；
is_share（true = 这是分享帖/自荐帖，作者在分享自己的东西而不是提问）；
重访帖另有 revisit / my_previous_reply / trigger_reason；media_codes 列出允许你使用的图文代码。
注意：你看不到帖子以外的实时信息，也不会联网。唯一的外部事实来源是 evidence 里的片段。
evidence 是关键词模糊检索得到的**候选**资料，可能包含与帖子无关的文档。用之前必须先判断
每条证据是否与帖子主题真正相关；不相关的证据一律不得引用、也不得基于它下结论，
此时按「evidence 为空」处理。每条 evidence 带一个 relevance（0~1）粗粒度主题相关度，
它只是标题字面重合的近似值，**仅供参考、不能当作相关性证明**：低分大概率跑题，
高分也可能因为共享「绿联NAS」这类通用词而虚高。最终是否相关由你根据标题和内容判断。

【事实纪律 —— 最重要】
- evidence 为空，或证据不足以支持某个结论时，**禁止**输出确定性结论，禁止报出具体版本号、固件号、
  参数值、端口数值、"官方已经支持/不支持 X""某某版本才有这个功能"这类断言。
  此时只能：追问一个最关键的缺失信息，或给出不带具体结论的排查顺序（先查什么、再查什么），
  并如实说明"这个我不太确定"。
-   只有 evidence 中**明确写了**的事实，才能作为结论写进回复；不要外推、不要脑补，
  不要把 "某文档提到 A" 说成 "所有机型都支持 A"。引用时说法要留有余地，例如"官方文档里提到…"。
- confidence 只允许三档，按证据强度如实填写：
  * "high"：evidence 里有直接、明确的依据，且你在 evidence_used 里列出对应 ref。
  * "medium"：只有间接线索，或仅凭通用经验给排查方向。
  * "low"：信息不足、纯鼓励、或你不确定。
- answer_mode 只能是 "direct"（给出结论）、"probe"（追问/排查方向）、"emoji"（纯表情鼓励）。
  **只有 confidence="high" 且 evidence_used 非空时才允许 "direct"。** 其余一律 "probe" 或 "emoji"。
- 绝不编造产品功能、版本号、参数或官方结论；不确定就明说不确定。宁可 should_reply=false，也不要答错。

【判断规则】
- 值得回复：具体报错、具体环境、明确需求、教程里可补充的细节、有信息量的经验讨论、写得好的原创教程。
- 不值得回复（should_reply=false，reply 留空）：广告/重复帖/官方公告制度帖；
  likely_solved=true；existing_replies 里别人已经给出了同样且正确的答案（不要复读）；
  帖子涉及政治敏感、色情、赌博、暴力、违法破解等话题；你完全没有相关知识的领域。
- 【重访帖】revisit=true 表示这帖你之前回过（my_previous_reply 是你当时说的话）。
  只有 trigger_reason 成立（有人点名你，或新增楼层出现了明显更优的解法）时才考虑回复，
  且**绝不能重复或改写 my_previous_reply**；其余情况一律 should_reply=false。
- 【水帖】纯签到、闲聊、打卡这类内容，允许用 kind="casual" 给一句简短友好的回应或表情包，
  但只在气氛轻松、确实没别的可补充时才用；每次运行的额度由脚本限制，别滥用。
- 帖子本身是纯表情/纯打卡内容时优先 casual；内容有价值时按下面的形态回。

【分享帖 / 自荐帖 / 教程帖 —— 要回一条】
- is_share=true 表示这是分享帖、自荐帖或教程帖：作者在分享自己的东西、写了一份教程，
  **不是在提问**，所以没有"答案"可给。
- 这类帖子**不要因为"没有技术问题可答"就跳过**。请用 kind="learn" 回一句
  学习 / 点赞 / 鼓励的话，可以是「赞」「学到了」「学习了」「nice」「感谢分享」
  「这个思路很妙」「看着不错，回头试试」这类，也可以带一个表情。
- **回复内容要贴着帖子的实际内容走**：提到帖子里具体的东西（工具名、做法、亮点），
  别写放之四海皆准的空话。比如教程贴就说"这个步骤讲得清楚，学到了"，
  自荐工具就说"看着不错，回头试试"。
- 但**不要**评价成技术结论，不要断言功能、版本、兼容性 —— 学习/点赞只表达
  「学到 / 欣赏 / 收藏 / 想试」。
- 只有下面这些情况才 should_reply=false：广告 / 重复帖 / 内容与标题无关 / 违规内容。
- 分享帖如果同时也能补充技术点，仍优先给 kind="text" 的实质回复；learn 只在没有实质内容可写时用。

【回复形态与条理】
- kind="text"：30~160 字，紧扣该帖的具体内容（提到设备、报错、环境或步骤）。
  **条理清晰**：先给一句结论或判断，再给依据或步骤；涉及多步操作时用
  `1. … 2. … 3. …` 或换行分点，并把 format 设为 "steps"；单段说明用 "plain"。
  不要写成没有层次的一大段，也不要堆砌术语。
  有证据时给可执行建议并说明依据；无证据时只给排查顺序或追问一个关键信息。
- kind="emoji"：帖子写得很完整、没有可补充的技术点但值得鼓励时，只回一个表情或「表情 + 不超过 6 字短语」，
  如 "👍"、"这个思路很巧 👏"、"[赞]"。整条不超过 20 字，此时 answer_mode="emoji"、confidence="low"、
  evidence_used=[]。不要为了凑数滥用表情。
- kind="casual"：水帖/闲聊的简短回应，2~20 字，可以是表情包、论坛表情码或一句轻松的话，
  如 "👍"、"[哈哈]"、"这个我也有同感"。answer_mode="casual"、confidence="low"、evidence_used=[]。
  仍禁止套话、广告、辱骂；**不要对技术提问使用 casual**。
- kind="learn"：分享帖 / 自荐帖 / 教程帖（is_share=true）的学习、点赞型回应，1~60 字，
  如「赞」「学到了 👍」「学习了，这个思路很妙」「nice」「感谢分享，已收藏」
  「「Compose」这个写法学到了」。
  answer_mode="learn"、confidence="low"、evidence_used=[]。
  **不要下技术结论、不要提版本号、不要给兼容性断言**，只表达学到 / 欣赏 / 收藏 / 想试。
- 【图文】需要配图或表情点缀时，可在 media 字段填一个允许的论坛代码（见输入里的 media_codes）。
  脚本只会附加白名单内的代码，其它一律忽略；不要在 reply 里写图片地址或任何链接。
  适度使用，一条回复最多一个，不得借此刷屏。

【通用要求】
- 口语、友好，不写"感谢分享""支持楼主""学习了"这类灌水句。
- **严禁**辱骂、人身攻击、歧视、嘲讽，以及涉及色情、赌博、暴力、违法破解或政治敏感的内容；
  遇到这类帖子直接 should_reply=false，不要接话、不要转述。
- 不假装官方、不推广、不留联系方式与外链；回复里不要写 ref 编号或图片链接；一次只输出一条回复，不要写标题。
- **不得编造、歪曲、夸大或断章取义**。引用证据要忠于原文：文档写「A 型号支持 X」，
  就不能写成「全系都支持 X」；把别人的经验说成官方结论也算歪曲。证据不足就明说不确定，宁可不回。

输出严格 JSON（不要任何解释性前后缀、不要 Markdown 围栏）：
{"results":[{"tid":"帖子ID","should_reply":true或false,
"kind":"text或emoji或casual或learn","format":"plain或steps",
"answer_mode":"direct或probe或emoji或casual或learn","confidence":"high或medium或low",
"evidence_used":["证据 ref，没有就空数组"],
"media":"允许的图文代码，不用就空字符串",
"reply":"回复正文","reason":"判断理由，20字内"}]}
tid 必须与输入的 tid 完全一致，每篇帖子恰好一条结果。"""

# 未配置 AI Key 时，只在演示/显式允许时使用；必须命中技术关键词才回
LOCAL_FALLBACK_RULES = (
    (("docker", "容器", "compose", "镜像", "端口映射"),
     "可以先看下容器的端口映射和日志：容器显示运行不代表服务已监听，"
     "进容器执行 netstat 或看应用日志，通常能定位是端口冲突还是应用没起来。"),
    (("硬盘", "休眠", "raid", "zfs", "存储池"),
     "硬盘频繁启停多半是后台有周期性读写，建议先查 SMART 日志和计划任务，"
     "把索引、缩略图生成这类任务错开，再观察休眠是否恢复正常。"),
    (("远程", "外网", "ddns", "内网穿透", "登录失败"),
     "远程访问建议按顺序排查：本机局域网是否通、NAS 端口是否映射、"
     "运营商是否有公网 IPv4，最后再看 DDNS 解析是否指向当前出口 IP。"),
    (("教程", "部署", "安装", "配置", "玩法"),
     "教程写得很清楚。补充一个容易踩的点：改配置前先备份原文件，"
     "升级后配置可能被覆盖，用 docker compose 管理会比手工改更省心。"),
)

DEFAULT_SUCCESS_MARKERS = ("succeedhandle", "回复发布成功", "帖子回复成功", "发布成功")
DEFAULT_FAIL_MARKERS = (
    "errorhandle", "抱歉", "无权", "没有权限", "请先登录", "您尚未登录",
    "主题已关闭", "两次发帖间隔", "内容太短", "请输入验证码", "验证问答",
    "禁止发言", "您所在的组", "请勿重复",
)

# AI 接入配置：厂商预设 + 环境变量别名（按优先级取第一个非空值）

DEFAULT_AI_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_AI_MODEL = "deepseek-ai/DeepSeek-V3"

# AI_PROVIDER=xxx 一键填好 baseURL 与模型；显式变量始终优先于预设
AI_PROVIDER_PRESETS: dict[str, tuple[str, str]] = {
    "siliconflow": (DEFAULT_AI_BASE, DEFAULT_AI_MODEL),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:14b"),
    "lmstudio": ("http://127.0.0.1:1234/v1", "local-model"),
    "custom": ("", ""),
}

# 思考型模型：默认把 token 花在 reasoning 上，正文 JSON 反而写不完。
# 这些模型默认带上 enable_thinking=false，除非用户显式用 AI_DISABLE_THINKING=0 打开。
THINKING_MODEL_HINTS = ("xing", "qwen3", "qwq", "-r1", "reasoner", "thinking",
                         "deepseek-v3.1", "deepseek-v3.2", "hunyuan-a13b",
                         "step-", "glm-4.6", "kimi-k2-thinking")

AI_BASE_ALIASES = (
    "AI_BASE_URL", "UGREEN_AI_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE",
    "LLM_BASE_URL", "AI_API_BASE", "SILICONFLOW_BASE_URL", "DEEPSEEK_BASE_URL",
)

AI_KEY_ALIASES = (
    "AI_API_KEY", "UGREEN_AI_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY",
    "AI_TOKEN", "SILICONFLOW_API_KEY", "SF_API_KEY", "DEEPSEEK_API_KEY",
    "API_KEY",
)
AI_MODEL_ALIASES = (
    "AI_MODEL", "UGREEN_AI_MODEL", "OPENAI_MODEL", "LLM_MODEL",
    "AI_MODEL_NAME", "SILICONFLOW_MODEL", "SF_MODEL", "DEEPSEEK_MODEL",
)
AI_ENDPOINT_ALIASES = ("AI_ENDPOINT", "AI_CHAT_URL", "OPENAI_CHAT_URL", "LLM_ENDPOINT")
AI_PROVIDER_ALIASES = ("AI_PROVIDER", "LLM_PROVIDER", "UGREEN_AI_PROVIDER")
