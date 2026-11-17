#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绿联 NAS 社区 · AI 自动回复（青龙面板 / 本地均可运行）。

本文件由 tools/build_single.py 自动生成 —— 请勿直接修改，
要改逻辑请改 ugreen/ 包内对应模块，然后重新运行该脚本。

相对于原 ugreen_auto_reply 的修复要点：
  1. 不再用 a[href*="thread-"] 抓帖子（它只会命中页脚的 29/30/104 三个制度帖）。
     默认抓论坛导读（forum.php?mod=guide&view=newthread，不区分板块、按发布时间倒序），
     也可退回按板块 RSS 抓取；列表页仅作兜底且限定在 #threadlisttableid 容器内。
  2. 不再随机灌固定语料，改由 OpenAI 兼容的大模型读标题 + 正文后生成/决定是否回复。
  3. 不再以 HTTP 200 判定成功，改为解析 Discuz 响应里的成功/失败标记。
  4. 修复企业微信推送：正确区分 sendNotify / QYWX_KEY / QYWX_AM 三种渠道。
  5. 状态持久化去重、黑名单、限流、dry-run 默认开启。
  6. A：把帖子里已有的楼层回复一起喂给模型，避免重复回答 / 识别已解决帖。
  7. B：保守模式——没有证据不得下确定性结论、不得编造版本号与参数；
     不确定时只追问一个关键信息或给排查方向。
  8. D：可插拔检索层，默认查询绿联官方知识库（support.ugnas.com 同源公开只读接口），
     也可叠加本地知识库 / Tavily / Bing / SearXNG / 站内搜索 / 自定义端点，
     把检索到的片段作为证据注入 prompt，并要求回复标注引用了哪些证据。

默认只演练不发帖。确认无误后设置 UGREEN_DRY_RUN=0 才会真正回复。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
import re
from datetime import datetime
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlsplit
import json
from dataclasses import dataclass, field
import hashlib
from urllib.parse import urljoin
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote
from difflib import SequenceMatcher
import argparse
import random

# ===== core/deps =====
# 绿联 NAS 社区自动回复 —— 第三方依赖引导。
# 
# 青龙容器里一般能 pip；可用 UGREEN_AUTO_INSTALL_DEPS=0 关闭自动安装。
# 作为模块被导入（如单元测试）时，缺依赖会如实抛错，不擅自安装。





def _install_deps(packages: list[str]) -> bool:
    print(f"[依赖] 缺少 {', '.join(packages)}，尝试自动安装 ...", flush=True)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q",
             "--disable-pip-version-check", *packages],
            timeout=300,
        )
        importlib.invalidate_caches()
        print("[依赖] 安装完成", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[依赖] 自动安装失败：{exc}", flush=True)
        print("[依赖] 请手动安装：pip install -r requirements.txt", flush=True)
        return False


try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    if (__name__ == "__main__"
            and os.environ.get("UGREEN_AUTO_INSTALL_DEPS", "1").strip() != "0"):
        if not _install_deps(["requests", "beautifulsoup4"]):
            sys.exit(1)
        import requests
        from bs4 import BeautifulSoup
    else:  # 作为模块被导入（如单元测试）时，让错误如实抛出
        raise

try:
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"


# ===== core/constants =====
# 绿联 NAS 社区自动回复 —— 常量与提示词。




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
SCRIPT_DIR = Path(__file__).resolve().parent

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


# ===== util/envutil =====
# 绿联 NAS 社区自动回复 —— 环境变量与日志。






def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name) or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name) or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    v = env_str(name).lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off", "n")


def env_fids() -> list[int]:
    raw = env_str("UGREEN_FORUM_FIDS")
    if not raw:
        return list(DEFAULT_FORUMS)
    out = []
    for part in re.split(r"[,，\s]+", raw):
        if part.isdigit():
            out.append(int(part))
    return out or list(DEFAULT_FORUMS)


# ===== util/textutil =====
# 绿联 NAS 社区自动回复 —— 文本清洗与截断。





def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'),
                 ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&#215;", "x")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def clip_text(s: str, limit: int) -> str:
    """把文本压到 limit 个字符以内，超长时用 … 收尾。

    之前日志里的标题是直接 title[:22] 硬切，看起来像「帖子标题就这么长」，
    容易误判；加上省略号后一眼就能看出是显示层截断。
    limit <= 0 表示不限制。
    """
    s = (s or "").strip()
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:max(1, limit - 1)].rstrip() + "…"


def truncate_bytes(s: str, limit: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    tail = "…".encode("utf-8")
    cut = limit - len(tail)
    if cut <= 0:
        return b[:limit].decode("utf-8", errors="ignore")
    return b[:cut].decode("utf-8", errors="ignore").rstrip() + "…"


def split_markers(raw: str, default: tuple[str, ...]) -> tuple[str, ...]:
    parts = tuple(x.strip() for x in re.split(r"[,，]", raw) if x.strip())
    return parts or default


# ===== util/timeutil =====
# 绿联 NAS 社区自动回复 —— 时间与链接解析。





def parse_rss_date(text: str) -> Optional[datetime]:
    """RSS pubDate 是带 +0000 的 UTC，转成脚本运行的本地时区（无 tz 的 naive）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def tid_from_url(url: str) -> str:
    m = re.search(r"[?&]tid=(\d+)", url or "")
    return m.group(1) if m else ""


def parse_guide_time(text: str, abs_date: str = "",
                     now: Optional[datetime] = None) -> Optional[datetime]:
    """解析论坛导读页的时间。

    导读页的时间有两种呈现：
      1. `<span title="2026-11-17">27 分钟前</span>` —— title 给绝对日期，
         文本给相对时间（N 分钟前 / 半小时前 / N 小时前 / N 天前）；
      2. `<span title="2026-11-16">昨天 23:54</span>` —— title 给绝对日期，
         文本给「昨天/前天 HH:MM」这种钟点。

    两种都要能解析；title 优先，因为它绝对可靠。
    """
    text = (text or "").replace("\xa0", " ").strip()
    now = now or datetime.now()
    # 去掉「发表于」前缀
    text = re.sub(r"^发表于\s*", "", text)

    base: Optional[datetime] = None
    if abs_date:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                base = datetime.strptime(abs_date.strip(), fmt)
                break
            except ValueError:
                continue

    # 1) 相对时间：N 分钟前 / 半小时前 / N 小时前 / N 天前
    m = re.search(r"(\d+)\s*分钟前", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    if "半小时前" in text:
        return now - timedelta(minutes=30)
    m = re.search(r"(\d+)\s*小时前", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return now - timedelta(days=int(m.group(1)))
    if "刚才" in text or "片刻前" in text:
        return now

    # 2) 钟点：昨天 23:54 / 前天 09:14 / 12-16 23:54
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m and base is not None:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return base.replace(hour=hh, minute=mm, second=0)

    # 3) 只有绝对日期：当作当天 00:00
    if base is not None:
        return base.replace(hour=0, minute=0, second=0)
    return None


# ===== util/aiutil =====
# 绿联 NAS 社区自动回复 —— AI 配置解析工具。






def env_first(names: tuple[str, ...], default: str = "") -> tuple[str, str]:
    """按优先级取第一个非空环境变量，返回 (值, 变量名)。"""
    for name in names:
        v = env_str(name)
        if v:
            return v, name
    return default, ""


def mask_secret(s: str) -> str:
    """日志里只显示首尾几位，绝不打印完整密钥。"""
    s = (s or "").strip()
    if not s:
        return "(未配置)"
    if len(s) <= 8:
        return s[:2] + "****"
    return f"{s[:3]}****{s[-4:]}"


def normalize_ai_base(raw: str) -> str:
    """把各种写法的 baseURL 统一成「不带 /chat/completions」的 API 根。

    兼容：只写域名（自动补 /v1）、写了 /v1、写了自定义路径、
    以及直接粘贴完整的 .../chat/completions 地址。
    """
    s = (raw or "").strip().rstrip("/")
    if not s:
        return DEFAULT_AI_BASE
    if not re.match(r"^https?://", s, re.I):
        raise ValueError(f"baseURL 必须以 http:// 或 https:// 开头：{raw!r}")
    s = re.sub(r"/chat/completions/?$", "", s, flags=re.I)
    if not urlsplit(s).path or urlsplit(s).path == "/":
        s += "/v1"                       # OpenAI 兼容端点普遍带 /v1
    return s.rstrip("/")


def build_ai_endpoint(base: str, chat_path: str = "chat/completions") -> str:
    """由 API 根 + 自定义路径拼出最终 POST 地址。"""
    s = (base or "").strip().rstrip("/")
    p = (chat_path or "chat/completions").strip().strip("/")
    if s.endswith("/" + p):
        return s
    return s + "/" + p


def parse_kv_mapping(raw: str, label: str) -> dict[str, str]:
    """AI_EXTRA_HEADERS：支持 JSON 对象，或 `K: V; K2: V2`（分号/换行分隔）。"""
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items()}
    except json.JSONDecodeError:
        pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", s):
        part = part.strip()
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k, v = k.strip().strip('"\''), v.strip().strip('"\'')
        if k:
            out[k] = v
    if not out:
        raise ValueError(f"{label} 格式错误：请用 JSON 对象（如 "
                         '{"X-Foo":"bar"}）或 `K: V; K2: V2` 写法')
    return out


def parse_extra_body(raw: str) -> dict:
    """AI_EXTRA_BODY：必须是 JSON 对象，用于传厂商私有参数（如 enable_thinking）。"""
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI_EXTRA_BODY 必须是 JSON 对象：{exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("AI_EXTRA_BODY 必须是 JSON 对象，不是数组或字符串")
    return obj


# ===== core/models =====
# 绿联 NAS 社区自动回复 —— 数据结构与检索关键词：Thread / Reply / Evidence。






KNOWN_SEARCH_PROVIDERS = (
    "ugnas-kb", "local", "site", "tavily", "bing", "baidu",
    "searxng", "custom",
)

# 中文不分词，用一张常见技术词表兜底提取检索关键词
TECH_TERMS = (
    "硬盘休眠", "远程访问", "内网穿透", "端口映射", "端口冲突", "反向代理",
    "存储池", "快照", "备份", "恢复", "RAID", "ZFS", "硬盘", "SSD", "扩容",
    "Docker", "Compose", "容器", "镜像", "虚拟机", "SSH", "WebDAV", "Samba",
    "NFS", "SMB", "权限", "共享文件夹", "挂载", "刮削", "相册", "影音",
    "Jellyfin", "Emby", "Plex", "qBittorrent", "Transmission", "下载",
    "DDNS", "IPv6", "公网", "HTTPS", "证书", "域名", "UPnP", "网络",
    "固件", "升级", "重启", "登录失败", "无法访问", "识别不到", "掉盘",
    "风扇", "噪音", "功耗", "UPS", "外接", "USB", "手机", "客户端",
    "UGOS Pro", "UGOS", "Syncthing", "Alist", "Home Assistant", "Nginx",
)


def parse_search_providers(raw: str) -> tuple[str, ...]:
    out: list[str] = []
    for part in re.split(r"[,，\s]+", (raw or "").lower()):
        p = part.strip()
        if not p or p in ("none", "off", "0", "no"):
            continue
        if p not in KNOWN_SEARCH_PROVIDERS:
            log(f"  忽略未知检索源 {p}（可用：{', '.join(KNOWN_SEARCH_PROVIDERS)}）")
            continue
        if p not in out:
            out.append(p)
    return tuple(out)


@dataclass
class Reply:
    """帖子详情页里一条已有楼层回复。"""
    floor: str = ""
    author: str = ""
    when: str = ""
    text: str = ""
    is_op: bool = False

    def as_dict(self) -> dict:
        return {"floor": self.floor, "author": self.author,
                "when": self.when, "is_op": self.is_op, "text": self.text}


@dataclass
class Evidence:
    """一条检索证据。ref 是给模型引用的短编号，绝不写进回复正文。"""
    ref: str
    title: str = ""
    snippet: str = ""
    source: str = ""
    url: str = ""
    score: float = 0.0
    relevance: float = 0.0     # 与帖子主题的粗粒度相关度，供模型参考

    def as_dict(self) -> dict:
        return {"ref": self.ref, "title": self.title,
                "snippet": self.snippet, "source": self.source,
                "relevance": round(self.relevance, 2)}


def extract_keywords(t: Thread, max_kw: int = 6) -> list[str]:
    """从标题 + 首楼正文里挑出适合检索的词：技术词表优先，再补英文/数字型号。"""
    text = clean_text(f"{t.title} {t.content[:500]}").lower()
    found: list[str] = []
    for term in TECH_TERMS:
        if term.lower() in text and term not in found:
            found.append(term)
        if len(found) >= max_kw:
            return found[:max_kw]
    for w in re.findall(r"[a-z][a-z0-9_.+\-]{2,}|\d{3,}", text):
        w = w.strip("._-+")
        if (w and w not in SEARCH_STOPWORDS and len(w) <= 24
                and not any(w in f.lower() for f in found)
                and w not in found):
            found.append(w)
        if len(found) >= max_kw:
            break
    return found[:max_kw]


def title_bigrams(s: str) -> set[str]:
    """中文不分词，用标题的 2-gram 集合近似衡量主题重合度。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", (s or "").lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def evidence_relevance(ev: Evidence, t: Thread,
                       keywords: list[str]) -> float:
    """粗粒度相关性打分：标题 2-gram 重合 + 关键词是否落在证据标题里。

    官方知识库用的是模糊（fog）检索，输入「硬盘」会返回一堆沾边的文档。
    如果不先剔掉跑题的，模型很可能把不相关证据当成依据，比不检索更危险。
    这里只做「主题是否相关」的近似判断，不做语义事实校验。
    """
    a = title_bigrams(t.title)
    b = title_bigrams(ev.title)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    kws = [k.lower() for k in (keywords or [])[:3]]
    hit = sum(1 for k in kws if k in (ev.title or "").lower())
    kw_score = hit / max(1, len(kws)) if kws else 0.0
    return 0.65 * min(1.0, overlap * 2.2) + 0.35 * kw_score


def build_search_queries(t: Thread, keywords: list[str]) -> list[str]:
    """每篇最多两条查询：技术关键词组合 + 标题短语，避免把整段正文塞给搜索引擎。"""
    queries: list[str] = []
    if keywords:
        q = " ".join(keywords[:3]).strip()
        if q:
            queries.append(q)
    title_q = clean_text(re.sub(r"[【\[（(].*?[】\]）)]", " ", t.title or ""))
    title_q = re.sub(r"[^\w\u4e00-\u9fff\s\-.]", " ", title_q).strip()
    if len(title_q) >= 4:
        title_q = " ".join(title_q.split()[:8])
        if title_q and title_q not in queries:
            queries.append(title_q)
    return queries[:2]


def stable_ref(prefix: str, seed: str) -> str:
    """给网页类结果生成可复现的短编号（Python 内建 hash 每次运行会变）。"""
    h = hashlib.md5((seed or "").encode("utf-8")).hexdigest()[:10]
    return f"{prefix}:{h}"


def dig_json(obj, path: str):
    """按点分路径取嵌套字段，供自定义检索端点使用（如 organic_results）。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


@dataclass
class Thread:
    tid: str
    title: str = ""
    forum: str = ""
    fid: int = 0
    author: str = ""
    url: str = ""
    published: Optional[datetime] = None
    content: str = ""
    formhash: str = ""
    reply_action: str = ""
    can_reply: bool = False
    detail_ok: bool = False
    skip_reason: str = ""
    # A / D 的新增字段
    replies: list[Reply] = field(default_factory=list)
    likely_solved: bool = False
    evidence: list[Evidence] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    search_note: str = ""
    # 重访已回复帖
    revisit: bool = False
    my_previous_reply: str = ""
    revisit_reason: str = ""

    @property
    def when(self) -> str:
        return self.published.strftime("%m-%d %H:%M") if self.published else "时间未知"


# ===== forum/classify =====
# 绿联 NAS 社区自动回复 —— 帖子分类：分享帖 / 水贴 / 标题分词。







def is_emoji_reply(s: str) -> bool:
    """纯表情，或「表情 + 不超过 6 个字的短语」。"""
    if not EMOJI_PATTERN.search(s):
        return False
    remainder = EMOJI_PATTERN.sub("", s)
    remainder = re.sub(r"[\s，。！!、~～.·|'\"]", "", remainder)
    return len(remainder) <= 6


def is_share_post(t: Thread) -> bool:
    """判断是不是「分享帖 / 自荐帖 / 教程帖」——作者在分享自己的东西，而不是提问。

    判定顺序：
      1. 强求助语气（求助 / 求个 / 请问 / ？）→ 一律是提问帖，不算分享；
      2. 强分享标记（自荐、实战、教程、一览、手把手…）→ 算分享帖；
      3. 标题带方括号标记（【uglog】【介绍ZFS】）→ 算分享帖，
         但【学院体验】这类**分类标记**要排除；
      4. 弱求助语气（怎么 / 如何 / 报错…）→ 是提问帖；
      5. 其余（标题看不出倾向）→ **按提问帖处理**。

    第 5 步刻意保守：宁可漏回一条，也不能给真问题回一句「感谢分享」。
    「如何 / 怎么」在教程标题里极常见（「ZFS如何保护我的数据」），
    所以只有当标题没有强分享标记时才拿它们当提问信号。

    注意：这是**脚本层的兜底判定**，真正的判断以模型读内容后的结论为准；
    模型自己选择 kind="learn" 的帖子，即使这里判成提问帖也不会被拦。
    """
    title = t.title or ""
    if not title:
        return False
    if any(q in title for q in STRONG_QUESTION_MARKERS):
        return False
    if any(m in title for m in STRONG_SHARE_MARKERS):
        return True
    # 标题带方括号标记（如【uglog】【介绍ZFS】）也算分享/自荐的强信号，
    # 但【学院体验】【活动】这种分类标记要排除
    for raw in BRACKET_TAG_RE.findall(title):
        inner = raw[1:-1].strip()
        if inner and not any(w in inner for w in CATEGORY_TAG_WORDS):
            return True
    if any(q in title for q in WEAK_QUESTION_MARKERS):
        return False
    return False


def already_praised(t: Thread) -> bool:
    """楼层里是否已经有人做过「学习/点赞」式回应。

    已经有人回过「感谢分享」「学习了」这类话，再回一遍就是刷屏。
    """
    for r in t.replies:
        text = clean_text(r.text or "")
        if not text:
            continue
        if len(text) <= 30 and any(w in text for w in LEARN_PRAISE_WORDS):
            return True
    return False


def share_learn_reply(t: Thread) -> str:
    """给分享帖 / 自荐帖 / 教程帖生成一句学习、点赞式回应。

    优先从标题（其次正文开头）里抓一个具体的东西（工具名、项目名、做法），
    让回复显得真诚而不是空灌；抓不到就退回通用但不失礼的短句。
    同一帖多次调用结果稳定，避免重复。
    """
    title = clean_text(t.title or "")
    token = extract_title_token(title)
    if not token:
        # 标题里没有明显的东西名，就看正文开头
        token = extract_title_token(clean_text(t.content or "")[:200])
    # 自荐/推荐类的用「想试」，教程/分享类的用「学到」
    if any(k in title for k in ("自荐", "推荐", "安利")):
        tail = "看着不错，回头试试 👍"
    elif any(k in title for k in ("教程", "实战", "科普", "玩法", "方案")):
        tail = "这个思路不错，学到了 👍"
    else:
        tail = "感谢分享，学到了 👍"
    if token:
        return f"「{token}」{tail}"
    return f"感谢分享，{tail}"


# 标题里到处都会出现的通用词，不能拿来当「具体的东西」，否则每条回复都长一个样
GENERIC_TOKENS = frozenset(
    "nas ugos docker the and for you com www http https linux shell api app "
    "pc mac win windows web ui gpu cpu ram ssd hdd usb json yaml tcp udp "
    "ipv4 ipv6 vpn smb nfs ssh ftp ai nas pro max plus one two".split()
)
# 同样无意义的中文通用片段
GENERIC_CN_TOKENS = frozenset(
    "绿联 nas 玩法 分享 方案 教程 实战 经验 方法 步骤 使用 安装 部署 配置 "
    "设置 问题 求助 小白 大佬 折腾 记录 笔记 内容 东西 工具 包 插件 "
    "数据 时候 东西 方面 情况 效果 感觉 意思 内容 部分".split()
)
# 方括号标记里属于「分类标记」的（不是工具名），要排除
SHARE_TAG_WORDS = ("自荐", "分享", "推荐", "活动", "公告", "教程", "经验",
                    "讨论", "问答", "求助", "提问", "开箱", "测评", "评测")
# 中文片段开头常见的虚词/助词，出现在开头说明这不是一个名字
_CN_PARTICLES = ("的", "了", "把", "给", "和", "与", "就", "也", "都", "很",
                 "更", "最", "这", "那", "个", "们", "在", "从", "被", "让",
                 "用", "做", "写", "记", "说", "来", "去", "上", "下", "一",
                 "请", "求", "新", "老", "小", "大")


def _token_score(seg: str) -> int:
    """给一个候选片段打分：越像「产品名 / 工具名」分越高。"""
    if not seg or len(seg) < 2:
        return -1
    if seg.lower() in GENERIC_TOKENS or seg in GENERIC_CN_TOKENS:
        return -1
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in seg)
    has_latin = any(c.isascii() and c.isalpha() for c in seg)
    score = 0
    if has_latin and has_cjk:
        # 英文开头 + 中文（AI管家、ZFS特性）比中文开头 + 英文（玩转NAS）更像产品名
        score += 6 if seg[0].isascii() and seg[0].isalpha() else 4
    elif has_latin:
        score += 4
    else:
        score += 2
    if len(seg) > 8:
        return -1           # 太长的多半是描述句，不是名字
    # 含有通用词片段的降权（绿联NAS 这种）
    if any(w in seg for w in GENERIC_CN_TOKENS):
        score -= 3
    if seg[0] in _CN_PARTICLES or seg[-1] in _CN_PARTICLES:
        score -= 4
    return score


def _split_cn_candidates(run: str) -> list[str]:
    """把一段中文按虚词切开，得到「可能是名字」的短片段。

    例：「我的远程访问方案」→ 去掉开头的「我的」→ 「远程访问方案」。
    """
    parts = re.split(f"[{''.join(_CN_PARTICLES)}]", run)
    out = []
    for p in parts:
        p = p.strip()
        if len(p) >= 2:
            out.append(p)
    return out


def extract_title_token(title: str) -> str:
    """从标题里挑一个最能代表「这东西叫什么」的片段。

    优先级：
      1. 方括号里的非分类标记（如【uglog】）—— 通常就是工具/项目名；
      2. 多词的英文专名（如 Home Assistant、WorkBuddy）；
      3. 中英混排或中文的短片段（如 AI管家、远程访问）。
    排除通用词和虚词开头的片段，避免出现「「的日志与状态推送工具」」这种废话。
    """
    if not title:
        return ""
    # 1) 方括号标记：【uglog】→ uglog；【介绍ZFS】→ ZFS（去掉「介绍」这类前缀词）
    for raw in BRACKET_TAG_RE.findall(title):
        inner = raw[1:-1].strip()
        if not inner or len(inner) > 20:
            continue
        if any(w in inner for w in SHARE_TAG_WORDS):
            continue
        if inner.lower() in GENERIC_TOKENS:
            continue
        # 【介绍ZFS】这种前缀 + 专名：只取专名部分
        lat = re.search(r"[A-Za-z][A-Za-z0-9_.\-]{1,19}", inner)
        if lat and any(w in inner for w in ("介绍", "关于", "浅谈", "谈一谈")):
            return lat.group(0)
        return inner
    # 去掉方括号标记再往下找，否则「自荐」本身会被当成名字
    core = BRACKET_TAG_RE.sub(" ", title)
    # 2) 英文专名：允许空格连接的多个首字母大写词
    best = ""
    for cand in re.findall(r"[A-Z][A-Za-z0-9_.\-]{2,}(?:\s+[A-Z][A-Za-z0-9_.\-]{2,})*",
                           core):
        cand = cand.strip()
        if cand.lower() in GENERIC_TOKENS:
            continue
        if len(cand) > len(best):
            best = cand
    if best:
        return best
    # 3) 中文 / 中英混排片段：按分隔符切开，逐个打分取最高
    best_seg = ""
    best_score = 0
    for run in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,14}", core):
        for seg in _split_cn_candidates(run):
            score = _token_score(seg)
            if score > best_score or (score == best_score and len(seg) > len(best_seg)):
                best_seg, best_score = seg, score
    return best_seg


def select_per_forum_latest(threads: list[Thread], limit: int,
                            title_chars: int = 0) -> list[Thread]:
    """每个板块各自取最新的 limit 篇，避免活跃板块把名额全占满。

    RSS 本身按时间倒序返回，但跨板块合并后如果直接截断，会变成「谁最新谁上」。
    这里按 fid 分组后再截取，保证六个板块都有机会进入 AI 判断。
    title_chars 只影响日志里这一行的展示长度（0 = 完整显示），不影响帖子数据。
    """
    groups: dict[int, list[Thread]] = {}
    for t in threads:
        groups.setdefault(t.fid, []).append(t)
    buckets: list[list[Thread]] = []
    for fid in sorted(groups):
        items = sorted(groups[fid],
                       key=lambda x: x.published or datetime.min,
                       reverse=True)
        take = items[:limit]
        log(f"  板块 {DEFAULT_FORUMS.get(fid, fid)} 最新 {len(take)}/{len(items)} 篇"
            + (f"，最新：{clip_text(take[0].title, title_chars)}" if take else ""))
        if take:
            buckets.append(take)
    # 各板块轮流交错，避免回复名额被排序靠前的板块一口气占完
    picked: list[Thread] = []
    for idx in range(limit):
        for bucket in buckets:
            if idx < len(bucket):
                picked.append(bucket[idx])
    return picked


# ===== core/config =====
# 绿联 NAS 社区自动回复 —— 配置对象 Config。








# 配置 / 数据结构


@dataclass
class Config:
    cookie: str = ""
    fids: list[int] = field(default_factory=lambda: list(DEFAULT_FORUMS))
    # 帖子来源：guide = 论坛导读（不区分板块，按发布时间倒序）；forums = 按 fids 走各板块 RSS
    use_guide: bool = True
    guide_pages: int = 1
    guide_limit: int = 20
    hours: float = 24.0
    max_detail: int = 30
    max_replies: int = 5
    # 每日回复配额（跨运行累计，按服务器本地自然日）：0 = 不限制。
    # 计数写在状态文件里，只有实发成功才计数；用满了就整轮跳过。
    daily_quota: int = 10
    per_forum_limit: int = 4
    allow_emoji_reply: bool = True
    dry_run: bool = True
    state_dir: Path = SCRIPT_DIR
    min_delay: int = 60
    max_delay: int = 180
    request_delay: float = 1.0
    timeout: int = 30
    # AI 接入（全部可由环境变量覆盖，详见 README 第四节）
    ai_base: str = DEFAULT_AI_BASE
    ai_key: str = ""
    ai_model: str = DEFAULT_AI_MODEL
    ai_endpoint: str = ""            # 最终 POST 地址；为空时由 baseURL 推导
    ai_chat_path: str = "chat/completions"
    ai_auth_header: str = "Authorization"
    ai_auth_scheme: str = "Bearer"   # 置空即直接把 Key 作为头的值（如 api-key）
    ai_temperature: float = 0.6
    ai_max_tokens: int = 131072     # 0 = 不限制（请求体里不带 max_tokens 字段）
    ai_max_tokens_cap: int = 1048576  # 失败自适应放宽时的天花板，0 = 不设顶
    ai_top_p: Optional[float] = None
    ai_timeout: int = 120
    ai_batch: int = 5
    ai_json_mode: bool = True
    ai_retries: int = 3
    ai_batch_fallback: bool = True
    ai_disable_thinking: bool = False
    ai_headers: dict = field(default_factory=dict)
    ai_extra_body: dict = field(default_factory=dict)
    ai_system_prompt: str = SYSTEM_PROMPT
    ai_provider: str = ""
    ai_sources: dict = field(default_factory=dict)
    ai_enabled: bool = True
    local_fallback: bool = True
    use_list_fallback: bool = True
    skip_events: bool = False
    notify: bool = True
    mark_state: bool = True
    debug: bool = False
    success_markers: tuple[str, ...] = DEFAULT_SUCCESS_MARKERS
    fail_markers: tuple[str, ...] = DEFAULT_FAIL_MARKERS
    fetch_replies: bool = True
    max_floors: int = 8
    floor_chars: int = 300
    skip_solved: bool = True
    conservative: bool = True
    require_evidence: bool = True
    allow_casual_reply: bool = True
    max_casual: int = 1
    # 分享帖 / 自荐帖的学习、点赞型回复
    allow_learn_reply: bool = True
    max_learn: int = 3
    allow_media: bool = True
    media_codes: tuple[str, ...] = DEFAULT_MEDIA_CODES
    recheck_replied: bool = True
    recheck_limit: int = 3
    revisit_hours: float = 168.0
    my_username: str = ""
    # 日志里「最新：xxx」那行的标题显示长度，0 = 完整显示（默认不截断）
    log_title_chars: int = 0
    search_enabled: bool = True
    # 默认带联网源：未配各自 Key 时它们直接返回空、不发任何请求；
    # 一旦填了 Key，知识库查不到证据时就会自动联网补查。
    search_providers: tuple[str, ...] = ("ugnas-kb", "tavily", "baidu")
    search_max_queries: int = 8
    search_max_evidence: int = 4
    search_snippet_chars: int = 400
    search_total_chars: int = 1800
    search_min_relevance: float = 0.12
    search_language: str = KB_DEFAULT_LANGUAGE
    search_delay: float = 0.8
    search_timeout: int = 20
    kb_fetch_detail: bool = True
    kb_detail_limit: int = 1
    web_fallback_threshold: int = 1
    local_kb_dir: Path = SCRIPT_DIR / "knowledge"
    tavily_key: str = ""
    bing_key: str = ""
    searxng_url: str = ""
    baidu_search_key: str = ""
    baidu_search_url: str = "https://qianfan.baidubce.com/v2/ai_search/web_search"
    baidu_search_engine: str = "baidu_search"
    baidu_top_k: int = 5
    baidu_results_path: str = ""
    baidu_auth_header: str = "Authorization"
    baidu_auth_scheme: str = "Bearer"
    baidu_headers: dict = field(default_factory=dict)
    custom_search_url: str = ""
    custom_results_path: str = "results"
    custom_title_field: str = "title"
    custom_snippet_field: str = "content"
    custom_url_field: str = "url"
    custom_headers: dict = field(default_factory=dict)

    @property
    def search_ready(self) -> bool:
        return self.search_enabled and bool(self.search_providers)

    @property
    def state_file(self) -> Path:
        return self.state_dir / "ugreen_state.json"

    @property
    def chat_url(self) -> str:
        """直接 new Config() 时也能得到可用地址，单测/离线调用不会断。"""
        return self.ai_endpoint or build_ai_endpoint(
            normalize_ai_base(self.ai_base), self.ai_chat_path)

    @property
    def ai_ready(self) -> bool:
        """有 Key，或用户用自定义请求头提供了别的认证方式（如 Azure api-key）。"""
        return bool(self.ai_key) or any(
            k.lower() in ("authorization", "api-key", "x-api-key", "token")
            for k in self.ai_headers)

    @property
    def use_ai(self) -> bool:
        return self.ai_enabled and self.ai_ready

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        cfg = cls()
        cfg.cookie = env_str("UGREEN_COOKIE") or env_str("UGNAS_COOKIE")
        cfg.fids = env_fids()
        # 帖子来源：默认走论坛导读（不区分板块），UGREEN_USE_GUIDE=0 改回按板块 RSS
        cfg.use_guide = env_bool("UGREEN_USE_GUIDE", True)
        cfg.guide_pages = max(1, env_int("UGREEN_GUIDE_PAGES", 1))
        cfg.guide_limit = max(1, env_int("UGREEN_GUIDE_LIMIT", 20))
        cfg.hours = env_float("UGREEN_HOURS", 24.0)
        cfg.max_detail = max(1, env_int("UGREEN_MAX_DETAIL", 30))
        cfg.max_replies = max(1, env_int("UGREEN_MAX_REPLIES", 5))
        # 每日配额：0 = 不限制（只受单次 UGREEN_MAX_REPLIES 约束）
        cfg.daily_quota = max(0, env_int("UGREEN_DAILY_QUOTA", 10))
        cfg.per_forum_limit = max(1, env_int("UGREEN_PER_FORUM_LIMIT", 4))
        cfg.allow_emoji_reply = env_bool("UGREEN_ALLOW_EMOJI_REPLY", True)
        cfg.dry_run = env_bool("UGREEN_DRY_RUN", True)
        cfg.state_dir = Path(env_str("UGREEN_STATE_DIR") or SCRIPT_DIR)
        cfg.min_delay = max(0, env_int("UGREEN_MIN_DELAY", 60))
        cfg.max_delay = max(cfg.min_delay, env_int("UGREEN_MAX_DELAY", 180))
        cfg.request_delay = max(0.0, env_float("UGREEN_REQUEST_DELAY", 1.0))
        cfg.timeout = env_int("UGREEN_TIMEOUT", 30)

        provider, _ = env_first(AI_PROVIDER_ALIASES)
        provider = provider.lower()
        preset_base, preset_model = AI_PROVIDER_PRESETS.get(provider, ("", ""))
        cfg.ai_provider = provider or "内置默认(siliconflow)"
        cfg.ai_enabled = env_bool("AI_ENABLED", True)
        cfg.ai_sources = {}

        base, src = env_first(AI_BASE_ALIASES)
        cfg.ai_base = base or preset_base or DEFAULT_AI_BASE
        cfg.ai_sources["baseURL"] = (src or (f"AI_PROVIDER={provider}" if preset_base
                                             else "内置默认"))

        key, src = env_first(AI_KEY_ALIASES)
        cfg.ai_key = key
        cfg.ai_sources["apiKey"] = src or "未配置"

        model, src = env_first(AI_MODEL_ALIASES)
        cfg.ai_model = model or preset_model or DEFAULT_AI_MODEL
        cfg.ai_sources["model"] = (src or (f"AI_PROVIDER={provider}" if preset_model
                                           else "内置默认"))

        endpoint_raw, endpoint_src = env_first(AI_ENDPOINT_ALIASES)
        cfg.ai_chat_path = env_str("AI_CHAT_PATH") or cfg.ai_chat_path
        cfg.ai_auth_header = env_str("AI_AUTH_HEADER") or cfg.ai_auth_header
        cfg.ai_auth_scheme = env_str("AI_AUTH_SCHEME", cfg.ai_auth_scheme)
        cfg.ai_temperature = max(0.0, min(2.0, env_float("AI_TEMPERATURE", 0.6)))
        # AI_MAX_TOKENS=0 表示「不限制」：请求体里干脆不带 max_tokens 字段，
        # 让服务端按模型自身的最大输出长度处理。
        # 现在的模型动辄 128K~1M 上下文，所以下限给到 128K，避免思考过程把正文挤掉；
        # 万一服务端不接受这么大的值，调用层会自动去掉该字段重试。
        cfg.ai_max_tokens = max(0, env_int("AI_MAX_TOKENS", 131072))
        # 自适应放宽的天花板；0 = 不设顶（一直往上限外放）。
        cfg.ai_max_tokens_cap = max(0, env_int("AI_MAX_TOKENS_CAP", 1048576))
        top_p = env_str("AI_TOP_P")
        cfg.ai_top_p = float(top_p) if top_p else None
        cfg.ai_timeout = max(5, env_int("AI_TIMEOUT", 120))
        cfg.ai_batch = max(1, env_int("AI_BATCH_SIZE", 5))
        cfg.ai_json_mode = env_bool("AI_JSON_MODE", True)
        cfg.ai_retries = max(1, min(6, env_int("AI_RETRIES", 3)))
        cfg.ai_batch_fallback = env_bool("AI_BATCH_FALLBACK", True)
        # 思考型模型默认关掉思考：否则 token 全被 reasoning 吃掉，正文 JSON 写不完。
        # AI_DISABLE_THINKING 显式写了就听用户的。
        raw_disable = env_str("AI_DISABLE_THINKING")
        if raw_disable:
            cfg.ai_disable_thinking = env_bool("AI_DISABLE_THINKING", True)
        else:
            low_model = (cfg.ai_model or "").lower()
            cfg.ai_disable_thinking = any(
                h in low_model for h in THINKING_MODEL_HINTS)
        cfg.ai_headers = parse_kv_mapping(env_str("AI_EXTRA_HEADERS"),
                                          "AI_EXTRA_HEADERS")
        cfg.ai_extra_body = parse_extra_body(env_str("AI_EXTRA_BODY"))

        prompt = env_str("AI_SYSTEM_PROMPT")
        prompt_file = env_str("AI_SYSTEM_PROMPT_FILE")
        if prompt_file:
            try:
                prompt = Path(prompt_file).read_text(encoding="utf-8").strip() or prompt
            except OSError as exc:
                log(f"AI_SYSTEM_PROMPT_FILE 读取失败，忽略：{exc}")
        cfg.ai_system_prompt = prompt or SYSTEM_PROMPT
        prompt_extra = env_str("AI_PROMPT_EXTRA")
        if prompt_extra:
            cfg.ai_system_prompt += "\n\n【用户附加要求】\n" + prompt_extra

        cfg.local_fallback = env_bool("UGREEN_LOCAL_FALLBACK", True)
        cfg.use_list_fallback = env_bool("UGREEN_USE_LIST_FALLBACK", True)
        cfg.skip_events = env_bool("UGREEN_SKIP_EVENTS", False)
        cfg.notify = env_bool("UGREEN_NOTIFY", True)
        cfg.mark_state = env_bool("UGREEN_MARK_STATE", True)
        cfg.debug = env_bool("UGREEN_DEBUG", False)
        cfg.success_markers = split_markers(env_str("UGREEN_SUCCESS_MARKERS"), DEFAULT_SUCCESS_MARKERS)
        cfg.fail_markers = split_markers(env_str("UGREEN_FAIL_MARKERS"), DEFAULT_FAIL_MARKERS)

        cfg.fetch_replies = env_bool("UGREEN_FETCH_REPLIES", True)
        cfg.max_floors = max(0, env_int("UGREEN_MAX_FLOORS", 8))
        cfg.floor_chars = max(60, env_int("UGREEN_FLOOR_CHARS", 300))
        cfg.skip_solved = env_bool("UGREEN_SKIP_SOLVED", True)
        cfg.conservative = env_bool("UGREEN_CONSERVATIVE", True)
        cfg.require_evidence = env_bool("UGREEN_REQUIRE_EVIDENCE", True)
        cfg.allow_casual_reply = env_bool("UGREEN_ALLOW_CASUAL_REPLY", True)
        cfg.max_casual = max(0, env_int("UGREEN_MAX_CASUAL", 1))
        cfg.allow_learn_reply = env_bool("UGREEN_ALLOW_LEARN_REPLY", True)
        cfg.max_learn = max(0, env_int("UGREEN_MAX_LEARN", 3))
        cfg.allow_media = env_bool("UGREEN_ALLOW_MEDIA", True)
        codes = split_markers(env_str("UGREEN_MEDIA_CODES"), DEFAULT_MEDIA_CODES)
        cfg.media_codes = tuple(c for c in codes if c.startswith("[") and c.endswith("]"))
        cfg.recheck_replied = env_bool("UGREEN_RECHECK_REPLIED", True)
        cfg.recheck_limit = max(0, env_int("UGREEN_RECHECK_LIMIT", 3))
        cfg.revisit_hours = max(1.0, env_float("UGREEN_REVISIT_HOURS", 168.0))
        cfg.my_username = env_str("UGREEN_MY_USERNAME").lstrip("@")
        cfg.log_title_chars = max(0, env_int("UGREEN_LOG_TITLE_CHARS", 0))

        cfg.search_enabled = env_bool("UGREEN_SEARCH", True)
        cfg.search_providers = parse_search_providers(
            env_str("UGREEN_SEARCH_PROVIDERS") or "ugnas-kb,tavily,baidu")
        cfg.search_max_queries = max(0, env_int("UGREEN_SEARCH_MAX_QUERIES", 8))
        cfg.search_max_evidence = max(1, env_int("UGREEN_SEARCH_MAX_EVIDENCE", 4))
        cfg.search_snippet_chars = max(80, env_int("UGREEN_SEARCH_SNIPPET_CHARS", 400))
        cfg.search_total_chars = max(200, env_int("UGREEN_SEARCH_TOTAL_CHARS", 1800))
        cfg.search_min_relevance = max(0.0, min(1.0, env_float(
            "UGREEN_SEARCH_MIN_RELEVANCE", 0.12)))
        cfg.search_language = env_str("UGREEN_SEARCH_LANGUAGE") or KB_DEFAULT_LANGUAGE
        cfg.search_delay = max(0.0, env_float("UGREEN_SEARCH_DELAY", 0.8))
        cfg.search_timeout = max(5, env_int("UGREEN_SEARCH_TIMEOUT", 20))
        cfg.kb_fetch_detail = env_bool("UGREEN_KB_DETAIL", True)
        cfg.kb_detail_limit = max(0, env_int("UGREEN_KB_DETAIL_LIMIT", 1))
        cfg.web_fallback_threshold = max(0, env_int("UGREEN_WEB_FALLBACK_THRESHOLD", 1))
        cfg.local_kb_dir = Path(env_str("UGREEN_LOCAL_KB_DIR")
                                or SCRIPT_DIR / "knowledge")
        cfg.tavily_key = env_str("TAVILY_API_KEY")
        cfg.bing_key = (env_str("BING_SEARCH_KEY") or env_str("BING_API_KEY"))
        cfg.searxng_url = env_str("SEARXNG_URL")
        cfg.baidu_search_key = (env_str("BAIDU_SEARCH_KEY")
                                or env_str("QIANFAN_API_KEY")
                                or env_str("APPBUILDER_TOKEN")
                                or env_str("BAIDU_API_KEY"))
        cfg.baidu_search_url = (env_str("BAIDU_SEARCH_URL")
                                or cfg.baidu_search_url)
        cfg.baidu_search_engine = (env_str("BAIDU_SEARCH_ENGINE")
                                   or cfg.baidu_search_engine)
        cfg.baidu_top_k = max(1, env_int("BAIDU_SEARCH_TOP_K", 5))
        cfg.baidu_results_path = env_str("BAIDU_SEARCH_RESULTS_PATH")
        cfg.baidu_auth_header = env_str("BAIDU_AUTH_HEADER") or cfg.baidu_auth_header
        cfg.baidu_auth_scheme = env_str("BAIDU_AUTH_SCHEME", cfg.baidu_auth_scheme)
        cfg.baidu_headers = parse_kv_mapping(env_str("BAIDU_SEARCH_HEADERS"),
                                             "BAIDU_SEARCH_HEADERS")
        cfg.custom_search_url = env_str("UGREEN_SEARCH_CUSTOM_URL")
        cfg.custom_results_path = env_str("UGREEN_SEARCH_RESULTS_PATH") or "results"
        cfg.custom_title_field = env_str("UGREEN_SEARCH_TITLE_FIELD") or "title"
        cfg.custom_snippet_field = env_str("UGREEN_SEARCH_SNIPPET_FIELD") or "content"
        cfg.custom_url_field = env_str("UGREEN_SEARCH_URL_FIELD") or "url"
        cfg.custom_headers = parse_kv_mapping(env_str("UGREEN_SEARCH_CUSTOM_HEADERS"),
                                              "UGREEN_SEARCH_CUSTOM_HEADERS")

        # 命令行优先
        if args.fids:
            cfg.fids = [int(x) for x in re.split(r"[,，\s]+", args.fids) if x.isdigit()]
        if args.use_guide is not None:
            cfg.use_guide = args.use_guide
        if args.guide_pages is not None:
            cfg.guide_pages = max(1, args.guide_pages)
        if args.guide_limit is not None:
            cfg.guide_limit = max(1, args.guide_limit)
        if args.hours is not None:
            cfg.hours = args.hours
        if args.limit is not None:
            cfg.max_replies = args.limit
        if args.daily_quota is not None:
            cfg.daily_quota = max(0, args.daily_quota)
        if args.per_forum is not None:
            cfg.per_forum_limit = max(1, args.per_forum)
        if args.detail_limit is not None:
            cfg.max_detail = args.detail_limit
        if args.no_emoji:
            cfg.allow_emoji_reply = False
        if args.base_url:
            cfg.ai_base = args.base_url.strip().rstrip("/")
            cfg.ai_sources["baseURL"] = "--base-url"
        if args.api_key:
            cfg.ai_key = args.api_key.strip()
            cfg.ai_sources["apiKey"] = "--api-key"
        if args.endpoint:
            endpoint_raw = args.endpoint.strip().rstrip("/")
            endpoint_src = "--endpoint"
        if args.model:
            cfg.ai_model = args.model
            cfg.ai_sources["model"] = "--model"

        # 统一算出最终 POST 地址：显式 endpoint 优先，否则由 baseURL 推导
        cfg.ai_base = normalize_ai_base(cfg.ai_base)
        if endpoint_raw:
            cfg.ai_endpoint = endpoint_raw
            cfg.ai_sources["endpoint"] = endpoint_src
        else:
            cfg.ai_endpoint = build_ai_endpoint(cfg.ai_base, cfg.ai_chat_path)
            cfg.ai_sources["endpoint"] = "由 baseURL + AI_CHAT_PATH 推导"
        if not re.match(r"^https?://", cfg.ai_endpoint, re.I):
            raise ValueError(f"AI endpoint 必须是 http(s) URL：{cfg.ai_endpoint!r}")

        if args.dry_run:
            cfg.dry_run = True
        if args.live:
            cfg.dry_run = False
        if args.no_notify:
            cfg.notify = False
        if args.no_mark:
            cfg.mark_state = False
        if args.debug:
            cfg.debug = True
        if args.no_replies:
            cfg.fetch_replies = False
        if args.no_conservative:
            cfg.conservative = False
        if args.no_search:
            cfg.search_enabled = False
        if args.search_providers:
            cfg.search_providers = parse_search_providers(args.search_providers)
        # 演练模式默认不动水位，避免把「已处理」时间推走
        if cfg.dry_run and not args.mark:
            cfg.mark_state = False
        return cfg


# ===== forum/client =====
# 绿联 NAS 社区自动回复 —— 论坛抓取与发帖客户端。









def floor_author(div) -> str:
    """取楼层作者名。

    绿联社区用的是 comiis 皮肤：用户名在 `a.km_name` 里（虽然 style=display:none，
    但文本仍在 DOM 中，get_text 能取到）；而 div.authi 里第一个
    `a[href*="mod=space"]` 是**头像链接**（只有 <img>、没有文本），
    直接取会拿到空字符串，于是「楼主是否已解决」「是否点名我」全部失效。
    标准 Discuz 模板则用 `a.xw1`。这里按优先级依次尝试，并跳过空文本。
    """
    for sel in ('div.authi a.km_name', 'div.authi a.xw1',
                'div.authi a[href*="mod=space"]'):
        for a in div.select(sel):
            name = clean_text(a.get_text(" ", strip=True))
            if name:
                return name
    return ""


def parse_reply_floors(soup: BeautifulSoup, t: Thread,
                       cfg: Config, max_floors: int = 0) -> list[Reply]:
    """A：把详情页里已有的楼层回复抽出来（首楼除外）。

    页面结构已实测：div#post_<pid> 是每层楼，正文在 td.t_f#postmessage_<pid>，
    作者是 div.authi a[href*=mod=space]，楼层号是 a#postnum<pid>，
    精确时间在 em[id^=authorposton] span[title]。
    注意 div[id^="post_"] 还会匹配 post_rate_div_xxx（评分层），必须按 id 全数字过滤。

    max_floors > 0 时覆盖 cfg.max_floors（重访帖需要看更多新楼层）。
    """
    out: list[Reply] = []
    if not cfg.fetch_replies or cfg.max_floors <= 0:
        return out
    limit = max_floors if max_floors > 0 else cfg.max_floors

    floors: list[tuple] = []
    for div in soup.select('div[id^="post_"]'):
        pid = div.get("id", "")
        if not re.fullmatch(r"post_\d+", pid):
            continue
        td = div.select_one('td.t_f[id^="postmessage_"]')
        if td is not None:
            floors.append((div, td))
    if floors and not t.author:
        # 首楼作者即楼主。列表页兜底和重访帖拿不到 author，这里从详情页回填，
        # 否则 is_op（楼主是否已表示解决）永远为 False。
        t.author = floor_author(floors[0][0])
    floors = floors[1:]      # 第 0 层是首楼，正文已在 t.content

    used = 0
    for idx, (div, td) in enumerate(floors[:limit], 1):
        body = clean_text(td.get_text(" ", strip=True))
        body = REPLY_NOISE_RE.sub("", body).strip()
        body = re.sub(r"^引用[:：].{0,120}?(?=\s)", "", body).strip()
        if not body:
            continue
        room = min(cfg.floor_chars, REPLY_BUDGET_CHARS - used)
        if room <= 0:
            break
        if len(body) > room:
            body = body[:room].rstrip() + "…"
        used += len(body)

        au = floor_author(div)
        author = au
        num = div.select_one('a[id^="postnum"]')
        floor = clean_text(num.get_text(" ", strip=True)) if num else f"{idx}楼"
        ts = div.select_one('em[id^="authorposton"] span[title]')
        when = (ts.get("title", "") if ts else "")
        if not when:
            em = div.select_one('em[id^="authorposton"]')
            when = clean_text(em.get_text(" ", strip=True)) if em else ""

        is_op = bool(author and t.author and author == t.author)
        if is_op and any(k in body for k in SOLVED_MARKERS):
            t.likely_solved = True
        out.append(Reply(floor=floor, author=author, when=when,
                         text=body, is_op=is_op))
    return out


class UgnasClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": BASE_URL,
        }
        if cfg.cookie:
            headers["Cookie"] = cfg.cookie
        self.session.headers.update(headers)

    def _raw(self, url: str, tries: int = 3) -> Optional[bytes]:
        for attempt in range(tries):
            try:
                r = self.session.get(url, timeout=self.cfg.timeout)
                r.raise_for_status()
                return r.content
            except Exception as exc:  # noqa: BLE001
                if attempt == tries - 1:
                    log(f"  请求失败 {url} -> {exc}")
                    return None
                time.sleep(2 * (attempt + 1))
        return None

    def _soup(self, url: str) -> Optional[BeautifulSoup]:
        raw = self._raw(url)
        if raw is None:
            return None
        text = raw.decode("utf-8", errors="ignore")
        return BeautifulSoup(text, HTML_PARSER)

    @staticmethod
    def is_logged_in(soup: BeautifulSoup) -> bool:
        # 游客页有「立即注册」且没有退出链接；登录态必有 action=logout
        if soup.select_one('a[href*="action=logout"]'):
            return True
        um = soup.select_one("#um")
        return bool(um and "登录" not in um.get_text(" ", strip=True)[:40])

    def check_login(self) -> bool:
        soup = self._soup(BASE_URL)
        return bool(soup and self.is_logged_in(soup))

    def fetch_guide(self) -> list[Thread]:
        """抓论坛导读：不区分板块，按发布时间倒序。

        导读页（forum.php?mod=guide&view=newthread）每页 50 条，
        条目结构是 `#threadlisttableid li.kmlist`：
          a.kmtit[href*=tid]     → tid
          span.km_subject        → 标题
          .kmfoot a[mod=space]   → 作者
          .kmtime                → 时间（相对文本 + title 里的绝对日期）
          a.kmbg[href*=fid=]     → 板块名 + fid

        导读页没有正文摘要，正文靠后续抓详情页补上。
        """
        threads: dict[str, Thread] = {}
        now = datetime.now()
        for page in range(1, max(1, self.cfg.guide_pages) + 1):
            soup = self._soup(GUIDE_URL.format(page=page))
            if soup is None:
                break
            ul = soup.select_one("#threadlisttableid")
            if ul is None:
                break
            items = ul.select("li.kmlist")
            if not items:
                break
            count = 0
            for li in items:
                a = li.select_one("a.kmtit")
                if a is None:
                    continue
                tid = tid_from_url(a.get("href", ""))
                if not tid or tid in threads:
                    continue
                subj = li.select_one(".km_subject")
                title = clean_text(subj.get_text(" ", strip=True)) if subj else \
                    clean_text(a.get_text(" ", strip=True))

                # 板块：a.kmbg 的文本是板块名，href 里带 fid
                forum, fid = "", 0
                bg = li.select_one("a.kmbg")
                if bg is not None:
                    forum = clean_text(bg.get_text(" ", strip=True))
                    mf = re.search(r"[?&]fid=(\d+)", bg.get("href", ""))
                    if mf:
                        fid = int(mf.group(1))
                if not forum and fid:
                    forum = DEFAULT_FORUMS.get(fid, str(fid))

                # 作者：kmfoot 里指向个人空间的纯文字链接（排除头像 img）
                author = ""
                foot = li.select_one(".kmfoot")
                if foot is not None:
                    for x in foot.select('a[href*="mod=space"]'):
                        if x.select("img"):
                            continue
                        txt = clean_text(x.get_text(" ", strip=True))
                        if txt:
                            author = txt
                            break

                # 时间：文本是相对值，span[title] 是绝对日期
                published = None
                tm = li.select_one(".kmtime")
                if tm is not None:
                    span = tm.select_one("span")
                    published = parse_guide_time(
                        tm.get_text(" ", strip=True),
                        span.get("title", "") if span else "", now)

                threads[tid] = Thread(
                    tid=tid, title=title, forum=forum, fid=fid,
                    author=author, url=THREAD_URL.format(tid=tid),
                    published=published,
                )
                count += 1
            log(f"  导读 第 {page} 页 {count} 篇"
                f"（累计 {len(threads)} 篇 / 每页 {GUIDE_PAGE_SIZE} 条）")
            if count < GUIDE_PAGE_SIZE:
                break   # 已经到最后一页
            time.sleep(self.cfg.request_delay)
        return list(threads.values())

    def fetch_rss(self) -> list[Thread]:
        """主抓取通道：各板块 RSS 实时、pubDate 精确到秒、自带正文摘要。"""
        threads: dict[str, Thread] = {}
        for fid in self.cfg.fids:
            raw = self._raw(RSS_URL.format(fid=fid))
            if raw is None:
                continue
            count = 0
            try:
                root = ET.fromstring(raw)
                for item in root.iter("item"):
                    tid = tid_from_url(item.findtext("link") or "")
                    if not tid or tid in threads:
                        continue
                    t = Thread(
                        tid=tid,
                        title=clean_text(item.findtext("title") or ""),
                        forum=clean_text(item.findtext("category") or "")
                        or DEFAULT_FORUMS.get(fid, str(fid)),
                        fid=fid,
                        author=clean_text(item.findtext("author") or ""),
                        url=THREAD_URL.format(tid=tid),
                        published=parse_rss_date(item.findtext("pubDate") or ""),
                        content=clean_text(item.findtext("description") or "")[:3000],
                    )
                    threads[tid] = t
                    count += 1
            except Exception as exc:  # noqa: BLE001
                log(f"  RSS 解析异常 fid={fid}: {exc}")
            log(f"  板块 {DEFAULT_FORUMS.get(fid, fid)}(fid={fid}) RSS {count} 篇")
            if count == 0 and self.cfg.use_list_fallback:
                for t in self.fetch_list(fid):
                    threads.setdefault(t.tid, t)
            time.sleep(self.cfg.request_delay)
        return list(threads.values())

    def fetch_list(self, fid: int) -> list[Thread]:
        """兜底通道：只认 #threadlisttableid 容器内的链接。

        页面里的 #layer_bbrules 隐藏层、页脚都含 viewthread 链接（29/30/31/104
        等制度帖），泛化的 a[href*="mod=viewthread"] 会把它们一起收进来。
        """
        soup = self._soup(FORUM_URL.format(fid=fid))
        if soup is None:
            return []
        container = (soup.select_one("#threadlisttableid")
                     or soup.select_one("#threadlist"))
        if container is None:
            return []
        out: dict[str, Thread] = {}
        for a in container.select('a[href*="mod=viewthread"]'):
            tid = tid_from_url(a.get("href", ""))
            if not tid or tid in out:
                continue
            out[tid] = Thread(
                tid=tid,
                title=clean_text(a.get("title") or a.get_text(" ", strip=True)),
                forum=DEFAULT_FORUMS.get(fid, str(fid)),
                fid=fid,
                url=THREAD_URL.format(tid=tid),
            )
        log(f"  板块 fid={fid} 列表页兜底 {len(out)} 篇")
        return list(out.values())

    def fetch_detail(self, t: Thread, floors: int = 0) -> Thread:
        soup = self._soup(t.url)
        if soup is None:
            return t
        try:
            subj = soup.select_one("span#thread_subject")
            if subj and not t.title:
                t.title = clean_text(subj.get_text(" ", strip=True))

            td = soup.select_one('td.t_f[id^="postmessage_"]')
            if td is not None:
                for junk in td.select("div.attach_nopermission, div.attach_tips, "
                                     "script, style"):
                    junk.decompose()
                body = clean_text(td.get_text(" ", strip=True))
                body = re.sub(r"^马上注册.{0,80}?立即注册\s*[×x]?\s*", "", body)
                body = re.sub(r"^您需要\s*登录\s*才可以.{0,80}?立即注册\s*[×x]?\s*", "", body)
                if len(body) > len(t.content):
                    t.content = body[:3000]

            fh = soup.select_one('input[name="formhash"]')
            if fh is not None:
                t.formhash = fh.get("value", "")

            # 直接用页面给出的回复 action，自动带上 fid / extra / replysubmit 等参数
            form = soup.select_one("form#fastpostform")
            if form is not None and form.get("action"):
                t.reply_action = urljoin(BASE_URL, form.get("action"))

            page_text = soup.get_text(" ", strip=True)
            closed = any(k in page_text for k in ("该主题已关闭", "主题已关闭", "本帖已锁定"))
            t.can_reply = bool(t.formhash and t.reply_action and not closed)
            if closed:
                t.skip_reason = "主题已关闭"

            # A：已有楼层（同时判断楼主是否已经表示解决）
            if self.cfg.fetch_replies:
                t.replies = parse_reply_floors(soup, t, self.cfg, floors)
                if (t.likely_solved and self.cfg.skip_solved
                        and not t.skip_reason):
                    t.skip_reason = "楼主已在楼层里表示问题已解决"
            t.detail_ok = True
        except Exception as exc:  # noqa: BLE001
            log(f"  详情解析异常 tid={t.tid}: {exc}")
        return t

    def post_reply(self, t: Thread, message: str) -> tuple[bool, str, str]:
        if not t.formhash or not t.reply_action:
            return False, "缺少 formhash 或回复地址", ""
        data = {
            "formhash": t.formhash,
            "message": message,
            "posttime": int(time.time()),
            "subject": "",
            "wysiwyg": "0",
            "usesig": "1",
        }
        headers = {
            "Referer": t.url,
            "Origin": BASE_URL.rstrip("/"),
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
        }
        try:
            r = self.session.post(t.reply_action, data=data, headers=headers,
                                  timeout=self.cfg.timeout)
            r.encoding = "utf-8"
            body = r.text or ""
        except Exception as exc:  # noqa: BLE001
            return False, f"请求异常：{exc}", ""
        ok, why = classify_reply_response(body, r.status_code,
                                          self.cfg.success_markers,
                                          self.cfg.fail_markers)
        return ok, why, body[:500]


def classify_reply_response(body: str, status: int,
                            success_markers: tuple[str, ...],
                            fail_markers: tuple[str, ...]) -> tuple[bool, str]:
    """Discuz 失败（未登录 / 无权 / 锁帖 / 灌水限制）同样返回 HTTP 200。

    因此必须看响应体：先判失败标记，再判成功标记，都不命中一律视为失败。
    """
    if status >= 400:
        return False, f"HTTP {status}"
    text = body or ""
    for k in fail_markers:
        if k in text:
            return False, f"命中失败提示：{k}"
    for k in success_markers:
        if k in text:
            return True, f"命中成功标记：{k}"
    return False, "响应中未找到成功标记（可用 UGREEN_DEBUG=1 查看响应体）"


# ===== search/providers =====
# 绿联 NAS 社区自动回复 —— 检索层：各 provider 与 Retriever。









#
# 默认只开 ugnas-kb —— 绿联官方知识库 support.ugnas.com 网页端同源的公开只读
# 接口，零配置即可用。其余 provider 按需用 UGREEN_SEARCH_PROVIDERS 叠加。
# 所有 provider 都返回 list[Evidence]，失败一律降级为空，绝不中断主流程。


def kb_headers() -> dict[str, str]:
    return {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
            "Origin": "https://support.ugnas.com", "Referer": KB_HOME_URL}


def kb_text(raw: str) -> str:
    """知识库返回的标题/摘要/正文里带 <font> 高亮和 <p>。

    高亮标签必须直接删掉而不是替换成空格，否则「开启<font>硬盘休眠</font>功能」
    会被切成「开启 硬盘休眠 功能」，影响证据可读性与关键词匹配。
    """
    s = re.sub(r"</?font[^>]*>", "", raw or "", flags=re.I)
    return clean_text(s)


def kb_article_detail(article_id: str, cfg: Config) -> str:
    """取一篇官方文档的完整正文（HTML → 纯文本）。失败返回空串。"""
    try:
        r = requests.get(KB_ARTICLE_URL,
                         params={"articleInfoId": article_id,
                                 "language": cfg.search_language},
                         headers=kb_headers(), timeout=cfg.search_timeout)
        d = r.json()
        if d.get("code") != 200:
            return ""
        return kb_text((d.get("data") or {}).get("content") or "")
    except Exception as exc:  # noqa: BLE001
        log(f"  知识库文章详情获取失败 id={article_id}: {exc}")
        return ""


def search_ugnas_kb(query: str, cfg: Config, client=None,
                    fetch_detail: bool = True) -> list[Evidence]:
    """绿联官方知识库搜索。已实测参数与返回结构（2026-11）。"""
    params = {"currentPage": 0, "size": max(3, cfg.search_max_evidence),
              "keywords": query, "strategy": KB_STRATEGY,
              "language": cfg.search_language}
    r = requests.get(KB_SEARCH_URL, params=params, headers=kb_headers(),
                     timeout=cfg.search_timeout)
    d = r.json()
    if d.get("code") != 200:
        log(f"  知识库搜索返回 code={d.get('code')}：{str(d.get('message'))[:80]}")
        return []
    rows = (d.get("data") or {}).get("data") or []
    out: list[Evidence] = []
    for i, row in enumerate(rows):
        # 视频教程没有可引用的正文，跳过
        if row.get("videoId") and not row.get("articleInfoId"):
            continue
        aid = str(row.get("articleInfoId") or row.get("id") or i)
        title = kb_text(row.get("title") or "")
        summary = kb_text(row.get("aiSummary") or "")
        content = kb_text(row.get("contentText") or "")
        snippet = (summary + " " + content).strip() or content or summary
        out.append(Evidence(
            ref=f"kb:{aid}", title=title or f"知识库文章 {aid}",
            snippet=snippet, source="绿联官方知识库", url=KB_HOME_URL,
            score=10.0 - i,
        ))
    # 只对第一篇取完整正文，把证据做实；其余保留搜索摘要即可
    if fetch_detail and cfg.kb_fetch_detail and cfg.kb_detail_limit > 0 and out:
        for ev in out[:cfg.kb_detail_limit]:
            aid = ev.ref.split(":", 1)[-1]
            if not aid.isdigit():
                continue
            body = kb_article_detail(aid, cfg)
            if body:
                ev.snippet = (ev.snippet + " " + body).strip()
    return out


def search_local_kb(query: str, cfg: Config, client=None,
                    fetch_detail: bool = True) -> list[Evidence]:
    """本地知识库：读 knowledge/*.md|txt，按 Markdown 标题分块做词频打分。"""
    d = cfg.local_kb_dir
    if not d.is_dir():
        return []
    terms = [x.lower() for x in re.split(r"[\s,，]+", query) if len(x) > 1]
    if not terms:
        return []
    out: list[Evidence] = []
    try:
        files = sorted(list(d.glob("*.md")) + list(d.glob("*.txt")))
    except OSError:
        return []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunks = re.split(r"(?m)^#{1,4}\s+", text)
        for i, ch in enumerate(chunks):
            low = ch.lower()
            score = sum(low.count(term) for term in terms)
            if not score:
                continue
            lines = [x.strip() for x in ch.splitlines() if x.strip()]
            title = lines[0][:60] if lines else p.stem
            body = " ".join(lines[1:]) if len(lines) > 1 else " ".join(lines)
            out.append(Evidence(ref=f"local:{p.stem}:{i}", title=title,
                                snippet=body, source=f"本地知识库/{p.name}",
                                score=float(score)))
    out.sort(key=lambda e: e.score, reverse=True)
    return out[:cfg.search_max_evidence]


def search_tavily(query: str, cfg: Config, client=None,
                  fetch_detail: bool = True) -> list[Evidence]:
    if not cfg.tavily_key:
        return []
    r = requests.post("https://api.tavily.com/search",
                      json={"api_key": cfg.tavily_key, "query": query,
                            "max_results": cfg.search_max_evidence,
                            "search_depth": "basic"},
                      timeout=cfg.search_timeout)
    rows = (r.json() or {}).get("results") or []
    return [Evidence(ref=stable_ref("web", str(x.get("url") or x.get("title") or "")),
                     title=clean_text(x.get("title") or ""),
                     snippet=clean_text(x.get("content") or ""),
                     source="Tavily", url=str(x.get("url") or ""),
                     score=5.0 - i)
            for i, x in enumerate(rows)]


def search_bing(query: str, cfg: Config, client=None,
                fetch_detail: bool = True) -> list[Evidence]:
    if not cfg.bing_key:
        return []
    r = requests.get("https://api.bing.microsoft.com/v7.0/search",
                     params={"q": query, "count": cfg.search_max_evidence,
                             "setLang": cfg.search_language},
                     headers={"Ocp-Apim-Subscription-Key": cfg.bing_key,
                              "User-Agent": UA},
                     timeout=cfg.search_timeout)
    rows = ((r.json() or {}).get("webPages") or {}).get("value") or []
    return [Evidence(ref=stable_ref("bing", str(x.get("url") or x.get("name") or "")),
                     title=clean_text(x.get("name") or ""),
                     snippet=clean_text(x.get("snippet") or ""),
                     source="Bing", url=str(x.get("url") or ""),
                     score=5.0 - i)
            for i, x in enumerate(rows)]


def search_searxng(query: str, cfg: Config, client=None,
                   fetch_detail: bool = True) -> list[Evidence]:
    if not cfg.searxng_url:
        return []
    url = cfg.searxng_url.rstrip("/") + "/search"
    r = requests.get(url, params={"q": query, "format": "json",
                                  "language": cfg.search_language},
                     headers={"User-Agent": UA}, timeout=cfg.search_timeout)
    rows = (r.json() or {}).get("results") or []
    return [Evidence(ref=stable_ref("searx", str(x.get("url") or x.get("title") or "")),
                     title=clean_text(x.get("title") or ""),
                     snippet=clean_text(x.get("content") or ""),
                     source="SearXNG", url=str(x.get("url") or ""),
                     score=5.0 - i)
            for i, x in enumerate(rows)]


def search_custom(query: str, cfg: Config, client=None,
                  fetch_detail: bool = True) -> list[Evidence]:
    """自定义 HTTP 检索端点：URL 里的 {query} 会被替换，否则追加 ?q=。"""
    if not cfg.custom_search_url:
        return []
    url = cfg.custom_search_url
    params = {}
    if "{query}" in url:
        url = url.replace("{query}", quote(query))
    else:
        params = {"q": query}
    headers = {"User-Agent": UA, "Accept": "application/json"}
    headers.update(cfg.custom_headers)
    r = requests.get(url, params=params, headers=headers,
                     timeout=cfg.search_timeout)
    rows = dig_json(r.json(), cfg.custom_results_path) or []
    out = []
    for i, x in enumerate(rows):
        if not isinstance(x, dict):
            continue
        title = clean_text(str(dig_json(x, cfg.custom_title_field) or ""))
        snippet = clean_text(str(dig_json(x, cfg.custom_snippet_field) or ""))
        link = str(dig_json(x, cfg.custom_url_field) or "")
        if not (title or snippet):
            continue
        out.append(Evidence(ref=stable_ref("custom", link or title),
                            title=title, snippet=snippet,
                            source="自定义检索", url=link, score=4.0 - i))
    return out


def search_baidu(query: str, cfg: Config, client=None,
                 fetch_detail: bool = True) -> list[Evidence]:
    """百度检索：默认走百度智能云千帆 AI 搜索（网页端同源的公开搜索接口）。

    端点、认证头、结果数组路径都可配置，因此也能接 AppBuilder 或自建代理。
    未配 BAIDU_SEARCH_KEY 时直接返回空，不产生任何网络请求。
    """
    if not cfg.baidu_search_key:
        return []
    body = {
        "messages": [{"role": "user", "content": query}],
        "search_source": cfg.baidu_search_engine,
        "resource_type_filter": [{"type": "web", "top_k": cfg.baidu_top_k}],
    }
    scheme = (cfg.baidu_auth_scheme or "").strip()
    value = (f"{scheme} {cfg.baidu_search_key}".strip()
             if scheme else cfg.baidu_search_key)
    headers = {"Content-Type": "application/json", "User-Agent": UA,
               cfg.baidu_auth_header or "Authorization": value}
    headers.update(cfg.baidu_headers)
    r = requests.post(cfg.baidu_search_url, json=body, headers=headers,
                      timeout=cfg.search_timeout)
    data = r.json() or {}

    paths = ([cfg.baidu_results_path] if cfg.baidu_results_path
             else list(BAIDU_RESULT_PATHS))
    rows = None
    for path in paths:
        candidate = dig_json(data, path)
        if isinstance(candidate, list):
            rows = candidate
            break

    out: list[Evidence] = []
    for i, x in enumerate(rows or []):
        if not isinstance(x, dict):
            continue
        title = clean_text(str(x.get("title") or x.get("name") or ""))
        snippet = clean_text(str(x.get("content") or x.get("snippet")
                                 or x.get("summary") or x.get("abstract") or ""))
        link = str(x.get("url") or x.get("link") or "")
        if not (title or snippet):
            continue
        out.append(Evidence(ref=stable_ref("baidu", link or title),
                            title=title, snippet=snippet,
                            source="百度检索", url=link, score=5.0 - i))
    return out[:cfg.search_max_evidence]


def search_site(query: str, cfg: Config, client=None,
                fetch_detail: bool = True) -> list[Evidence]:
    """Discuz 站内搜索。游客态会被拒（实测返回「提示信息」页），仅在带 Cookie 时可用。"""
    if client is None or not cfg.cookie:
        return []
    url = (BASE_URL + "search.php?mod=forum&srchtxt=" + quote(query)
           + "&searchsubmit=yes")
    r = client.session.get(url, timeout=cfg.search_timeout)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, HTML_PARSER)
    out: list[Evidence] = []
    for i, li in enumerate(soup.select("li.pbw")[:cfg.search_max_evidence]):
        a = li.select_one("h3 a") or li.select_one("a[href*='mod=viewthread']")
        if a is None:
            continue
        tid = tid_from_url(a.get("href", ""))
        out.append(Evidence(ref=f"site:{tid or i}",
                            title=clean_text(a.get_text(" ", strip=True)),
                            snippet=clean_text(li.get_text(" ", strip=True))[:400],
                            source="社区站内搜索",
                            url=THREAD_URL.format(tid=tid) if tid else "",
                            score=3.0 - i))
    return out


# 存函数名而不是函数对象：运行时通过 globals() 解析，便于单元测试替换、
# 也方便后续在不改这里的情况下覆盖某个 provider。
SEARCH_FUNCS = {
    "ugnas-kb": "search_ugnas_kb",
    "local": "search_local_kb",
    "site": "search_site",
    "tavily": "search_tavily",
    "bing": "search_bing",
    "baidu": "search_baidu",
    "searxng": "search_searxng",
    "custom": "search_custom",
}


class Retriever:
    """按关键词检索、聚合去重、按总字数预算裁剪；同一次运行内做结果缓存。"""

    def __init__(self, cfg: Config, client=None):
        self.cfg = cfg
        self.client = client
        self.cache: dict[tuple[str, str], list[Evidence]] = {}
        self.calls = 0
        self.errors: list[str] = []

    def _one(self, provider: str, query: str) -> list[Evidence]:
        key = (provider, query)
        if key in self.cache:
            return list(self.cache[key])
        fn = globals()[SEARCH_FUNCS[provider]]
        try:
            res = list(fn(query, self.cfg, self.client) or [])
        except Exception as exc:  # noqa: BLE001
            msg = f"{provider}: {type(exc).__name__} {str(exc)[:90]}"
            self.errors.append(msg)
            log(f"  检索源 {provider} 失败（已降级）：{msg}")
            res = []
        self.calls += 1
        self.cache[key] = res
        if self.cfg.search_delay:
            time.sleep(self.cfg.search_delay)
        return list(res)

    def _run_providers(self, providers: list[str], queries: list[str],
                       merged: dict[str, Evidence]) -> list[str]:
        notes: list[str] = []
        for provider in providers:
            got = 0
            for q in queries:
                for ev in self._one(provider, q):
                    if ev.ref not in merged or ev.score > merged[ev.ref].score:
                        merged[ev.ref] = ev
                        got += 1
            if got:
                notes.append(f"{provider}={got}")
        return notes

    def _relevant(self, merged: dict[str, Evidence], t: Thread) -> list[Evidence]:
        kept = []
        for ev in merged.values():
            ev.relevance = evidence_relevance(ev, t, t.keywords)
            if ev.relevance >= self.cfg.search_min_relevance:
                kept.append(ev)
        return sorted(kept, key=lambda e: e.score, reverse=True)

    def collect(self, t: Thread, queries: list[str]) -> list[Evidence]:
        merged: dict[str, Evidence] = {}
        primary = [p for p in self.cfg.search_providers if p in KB_FIRST_PROVIDERS]
        secondary = [p for p in self.cfg.search_providers if p in WEB_PROVIDERS]
        # 先查一手资料（官方知识库 / 本地 / 站内），明显不够时才动用联网源。
        # 没配 Key 的联网 provider 会直接返回空，不会产生任何请求。
        per_source = self._run_providers(primary, queries, merged)
        kept = self._relevant(merged, t)
        if len(kept) < self.cfg.web_fallback_threshold and secondary:
            per_source += self._run_providers(secondary, queries, merged)
            per_source.append("联网补查")
            kept = self._relevant(merged, t)
        t.search_note = " ".join(per_source) or "无命中"
        # 先剔掉跑题的候选，否则模型会把「沾边」文档当依据
        dropped = len(merged) - len(kept)
        if dropped:
            t.search_note += f"，低相关丢弃{dropped}"
        out: list[Evidence] = []
        used = 0
        for ev in kept[:self.cfg.search_max_evidence]:
            room = min(self.cfg.search_snippet_chars,
                       self.cfg.search_total_chars - used)
            if room <= 0:
                break
            snippet = kb_text(ev.snippet)
            if len(snippet) > room:
                # 省略号本身也占一个字，否则总字数会超出预算
                snippet = snippet[:max(0, room - 1)].rstrip() + "…"
            used += len(snippet)
            out.append(Evidence(ref=ev.ref, title=ev.title[:80],
                                snippet=snippet, source=ev.source,
                                url=ev.url, score=ev.score,
                                relevance=ev.relevance))
        return out


def describe_evidence(evidences: list[Evidence]) -> str:
    return (" / ".join(f"{e.ref}《{e.title[:20]}》[{e.relevance:.2f}]"
                       for e in evidences) or "无")


# ===== ai/quality =====
# 绿联 NAS 社区自动回复 —— 回复质检：去重、套话、事实性红线。









def local_fallback_reply(t: Thread) -> tuple[bool, str, str]:
    """无 AI Key 时的保守兜底，返回 (是否回复, 内容, 形态)。

    只在标题命中明确技术关键词时给排查方向；长篇分享帖若没有可补充的点，
    就只回一个表情，用来演示 kind=emoji 链路。
    """
    text = f"{t.title} {t.content[:200]}".lower()
    # 分享帖 / 自荐帖：回一句学习、点赞（演示 kind=learn 链路）
    if is_share_post(t):
        return True, share_learn_reply(t), "learn"
    # 长篇教程/科普帖本身已完整，没有可补充的点，先判成表情回复
    if len(t.content) >= 300 and any(
            k in t.title for k in ("一览", "分享", "实战", "体验", "盘点", "科普")):
        return True, "👍", "emoji"
    for words, reply in LOCAL_FALLBACK_RULES:
        if any(w.lower() in text for w in words):
            return True, reply, "text"
    return False, "", "text"


def sanitize_reply(text: str, recent: list[str],
                   kind: str = "text",
                   allow_emoji: bool = True,
                   media: str = "",
                   media_codes: tuple[str, ...] = (),
                   allow_casual: bool = True,
                   share_post: bool = False) -> tuple[bool, str]:
    """发出去之前的最后一道闸：内容安全 + 灌水 + 长度 + 重复 + 图文白名单。

    kind=emoji / casual 时放宽长度；但辱骂、不当内容、套话、外链、联系方式、
    AI 自述一律仍拦，且不允许连续三次刷同一个表情。

    固定语料（感谢分享 / 赞一个 / 学习了…）的放行条件：
      * kind="learn" —— 学习/点赞型回复本身就是这些语料；
      * share_post=True —— 分享帖 / 自荐帖，作者在分享自己的东西，
        回一句「感谢分享」是正常互动，不是灌水。
    技术性回复（kind=text 且非分享帖）里仍然拦，避免刷屏。
    """
    s = (text or "").strip().strip('"“”').strip()
    s = re.sub(r"^```.*?$|```", "", s, flags=re.M).strip()
    if not s:
        return False, "空回复"
    if ABUSIVE_RE.search(s) or UNSAFE_TOPIC_RE.search(s):
        return False, "含辱骂 / 违背公序良俗的内容"
    if BAD_REPLY_RE.search(s):
        return False, "含外链 / 联系方式 / AI 自述"
    if kind != "learn" and not share_post:
        for cliche in CLICHE_REPLIES:
            if cliche in s:
                return False, f"命中灌水套话：{cliche}"

    # 图文：只接受配置白名单里的论坛代码，其它一律静默忽略
    extra = ("\n" + media) if (media and media in (media_codes or ())) else ""

    casual_like = (allow_casual and kind == "casual" and len(s) <= 20)
    emoji_like = (allow_emoji and len(s) <= 20
                  and (is_emoji_reply(s) or kind == "emoji"))
    if casual_like:
        if len(s) < 2:
            return False, "水帖回复过短"
        if len(recent) >= 3 and all(x.strip() == s for x in recent[-3:]):
            return False, "连续重复同一句"
        return True, s + extra

    if emoji_like:
        if not EMOJI_PATTERN.search(s):
            return False, "标记为表情回复，但正文里没有表情"
        if len(recent) >= 3 and all(x.strip() == s for x in recent[-3:]):
            return False, "连续重复同一个表情"
        return True, s + extra

    # 学习/点赞型：允许套话，但要有实质内容且不能和近期回复雷同
    if kind == "learn":
        if len(s) < 1:
            return False, "学习型回复为空"
        if len(s) > 60:
            return False, f"学习型回复过长（{len(s)} 字）"
        for old in recent:
            if SequenceMatcher(None, s, old).ratio() > 0.92:
                return False, "与近期已发回复高度重复"
        return True, s + extra

    if not (8 <= len(s) <= 200):
        return False, f"长度不合规（{len(s)} 字）"
    for old in recent:
        if SequenceMatcher(None, s, old).ratio() > 0.92:
            return False, "与近期已发回复高度重复"
    return True, s + extra


def validate_claim(reply: str, d: dict, t: Thread,
                   cfg: Config) -> tuple[bool, str]:
    """B：保守模式的硬闸——提示词只是指令，这里才真正拦住无据断言。

    注意：它只能校验「有没有引用真实证据、有没有出现版本号/确定性措辞」，
    无法判断回复与证据在语义上是否一致；语义一致性仍依赖模型遵守提示词。
    """
    if ABUSIVE_RE.search(reply) or UNSAFE_TOPIC_RE.search(reply):
        return False, "含辱骂 / 违背公序良俗的内容"
    # 重访帖：内容与上次回复几乎一样就等于没回，直接拦掉
    if t.revisit and t.my_previous_reply:
        if SequenceMatcher(None, reply, t.my_previous_reply).ratio() > 0.85:
            return False, "与上次回复几乎一样，不做重复回复"
    if not cfg.conservative:
        return True, ""
    kind = d.get("kind", "text")
    emoji = kind in ("emoji", "casual", "learn") or is_emoji_reply(reply)
    valid_refs = {e.ref for e in t.evidence}
    used = [r for r in d.get("evidence_used", []) if r in valid_refs]
    bogus = [r for r in d.get("evidence_used", []) if r not in valid_refs]
    mode = d.get("answer_mode", "probe")
    confidence = d.get("confidence", "low")

    if bogus:
        return False, f"引用了不存在的证据编号：{bogus[0]}"
    if emoji:
        if VERSION_RE.search(reply) or RISK_CLAIM_RE.search(reply):
            return False, "表情回复里夹带了技术断言"
        return True, ""

    if mode == "direct" or confidence == "high":
        if not t.evidence:
            return False, "没有检索证据却要下确定性结论"
        if cfg.require_evidence and not used:
            return False, "声称有依据但未引用任何有效证据编号"
        if confidence != "high":
            return False, "confidence 不是 high 却要下确定性结论"

    if not used:
        if VERSION_RE.search(reply):
            return False, "无证据却给出具体版本号 / 参数"
        if RISK_CLAIM_RE.search(reply):
            return False, "无证据却下确定性结论或使用官方口径"
    return True, ""


# ===== ai/client =====
# 绿联 NAS 社区自动回复 —— AI 介入：构造请求、解析响应、生成回复。













def ai_payload(threads: list[Thread],
               cfg: Optional[Config] = None) -> list[dict]:
    """喂给模型的全部信息：首楼 + 已有楼层 + 检索证据。除此之外模型一无所知。"""
    codes = list(cfg.media_codes if cfg else DEFAULT_MEDIA_CODES)
    return [{
        "tid": t.tid,
        "title": t.title,
        "forum": t.forum,
        "author": t.author,
        "published": t.when,
        "content_preview": t.content[:800],
        "existing_replies": [r.as_dict() for r in t.replies[:8]],
        "likely_solved": t.likely_solved,
        "keywords": t.keywords,
        "evidence": [e.as_dict() for e in t.evidence],
        "revisit": t.revisit,
        "my_previous_reply": t.my_previous_reply[:200],
        "trigger_reason": t.revisit_reason,
        "is_share": is_share_post(t),
        "media_codes": codes,
    } for t in threads]


def ai_request_headers(cfg: Config) -> dict[str, str]:
    """认证头可由 AI_AUTH_HEADER / AI_AUTH_SCHEME / AI_EXTRA_HEADERS 完全自定义。"""
    headers = {"Content-Type": "application/json"}
    if cfg.ai_key:
        scheme = (cfg.ai_auth_scheme or "").strip()
        value = f"{scheme} {cfg.ai_key}".strip() if scheme else cfg.ai_key
        headers[cfg.ai_auth_header or "Authorization"] = value
    headers.update(cfg.ai_headers)
    return headers


def ai_request_body(batch: list[Thread], cfg: Config) -> dict:
    body = {
        "model": cfg.ai_model,
        "messages": [
            {"role": "system", "content": cfg.ai_system_prompt},
            {"role": "user", "content":
                "以下是 %d 篇待回复的帖子，请逐篇判断并输出 JSON：\n\n%s"
                % (len(batch), json.dumps(ai_payload(batch, cfg),
                                          ensure_ascii=False, indent=1))},
        ],
        "temperature": cfg.ai_temperature,
    }
    # ai_max_tokens = 0 表示不限制：直接不带 max_tokens 字段，
    # 由服务端按模型自身的最大输出长度决定。
    if cfg.ai_max_tokens > 0:
        body["max_tokens"] = cfg.ai_max_tokens
    if cfg.ai_top_p is not None:
        body["top_p"] = cfg.ai_top_p
    if cfg.ai_json_mode:
        body["response_format"] = {"type": "json_object"}
    if cfg.ai_disable_thinking and "enable_thinking" not in body:
        # 思考型模型：先关掉思考，保证 max_tokens 全给正文
        body["enable_thinking"] = False
    body.update(cfg.ai_extra_body)   # 厂商私有参数，或强制覆盖任一字段
    return body


def call_ai(batch: list[Thread], cfg: Config) -> Optional[dict]:
    """调模型并解析 JSON。

    思考型模型（SiliconFlow 的 Xing 系列、Qwen3 等）常见两种翻车：
      1) 在 JSON 前面加一段说明文字，或套一层 ```json 围栏；
      2) token 全花在思考过程上，正文是空的（finish_reason=length）。
    这里对两种都做兜底：JSON 提取见 extract_json_payload，
    失败时按情况放宽 max_tokens / 关闭思考模式 / 追加严格格式要求，
    并把原始返回片段打出来，方便定位。
    """
    url = cfg.chat_url
    body = ai_request_body(batch, cfg)
    headers = ai_request_headers(cfg)
    valid = {t.tid for t in batch}

    for attempt in range(cfg.ai_retries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=cfg.ai_timeout)
            if (r.status_code == 400 and "response_format" in (r.text or "")
                    and "response_format" in body):
                body.pop("response_format", None)  # 部分自建端点不支持 json_object
                cfg.ai_extra_body.pop("response_format", None)
                cfg.ai_json_mode = False
                log("  模型不支持 response_format，已改为普通 JSON 输出重试")
                continue
            if (r.status_code == 400 and "enable_thinking" in body
                    and "enable_thinking" in (r.text or "")):
                body.pop("enable_thinking", None)  # 该端点不认这个参数，去掉再试
                log("  模型接口不认 enable_thinking 参数，已去掉并重试")
                continue
            if (r.status_code == 400 and body.get("max_tokens")
                    and ("max_tokens" in (r.text or "")
                         or "max tokens" in (r.text or "").lower()
                         or "maximum context" in (r.text or "").lower())):
                # 部分模型/网关不接受这么大的 max_tokens（超出模型自身上限），
                # 直接去掉该字段，让服务端按默认值处理。
                dropped = body.pop("max_tokens")
                cfg.ai_max_tokens = 0
                cfg.ai_max_tokens_cap = 0
                log(f"  模型接口不接受 max_tokens={dropped}，已去掉该参数并重试"
                    "（不限制输出长度）")
                continue
            if r.status_code != 200:
                log(f"  模型接口 {r.status_code}: {r.text[:200]}")
                if r.status_code in (401, 403):
                    return None
                time.sleep(3 * (attempt + 1))
                continue
            try:
                resp = r.json()
            except json.JSONDecodeError:
                log(f"  模型接口返回的不是 JSON（第 {attempt + 1} 次）：{r.text[:200]}")
                time.sleep(2)
                continue

            content, meta = extract_ai_content(resp)
            try:
                return parse_ai_response(content, valid)
            except json.JSONDecodeError as exc:
                log(f"  模型返回不是合法 JSON（第 {attempt + 1} 次）：{exc}")
                log(f"    finish_reason={meta.get('finish_reason')} "
                    f"正文长度={meta.get('content_len')} "
                    f"响应长度={meta.get('raw_len')}")
                raw = (content or "").strip()
                if raw:
                    log(f"    原始返回片段：{raw[:200]}")
                else:
                    log("    原始返回为空：模型很可能只输出了思考过程，正文没生成")
                if meta.get("finish_reason") == "length":
                    # 输出被截断。根因通常不是额度不够，而是思考过程把 token 吃光，
                    # 所以先关思考（治本），再考虑放宽 max_tokens（治标）。
                    thinking_already_off = "enable_thinking" in body
                    if not thinking_already_off:
                        body["enable_thinking"] = False
                        # 记住要关思考，后续批次与逐篇重试都沿用
                        cfg.ai_disable_thinking = True
                        log("    下一轮关闭思考模式（enable_thinking=false）")
                    if body.get("max_tokens"):
                        old = int(body["max_tokens"])
                        new = int(old * 1.6) + 256
                        if cfg.ai_max_tokens_cap > 0:
                            new = min(new, cfg.ai_max_tokens_cap)
                        body["max_tokens"] = new
                        # 记住这次放宽，后续批次与逐篇重试都沿用
                        cfg.ai_max_tokens = new
                        log(f"    输出被 max_tokens 截断，下一轮放宽到 {new}")
                    elif thinking_already_off:
                        # 已经是不限制模式、思考也关掉了还是被截断，
                        # 说明是服务端自己的输出上限，再试同样大小的批次没意义，
                        # 交给逐篇重试把批次拆小。
                        log("    输出仍被截断（未限制 max_tokens 且已关闭思考，"
                            "疑似服务端输出上限），本批不再重试，改由逐篇重试拆分")
                        return None
                if meta.get("reasoning_only") and "enable_thinking" not in body:
                    body["enable_thinking"] = False
                    # 记住要关思考，后续批次与逐篇重试都沿用
                    cfg.ai_disable_thinking = True
                    log("    下一轮关闭思考模式（enable_thinking=false）")
                if attempt + 1 < cfg.ai_retries:
                    body = _strict_nudge(body)
                time.sleep(2)
                continue
        except Exception as exc:  # noqa: BLE001
            log(f"  调用模型异常（第 {attempt + 1} 次）：{exc}")
            time.sleep(3 * (attempt + 1))
    return None


def describe_ai_config(cfg: Config) -> str:
    """--show-config 用：把生效值与「来自哪个环境变量」一起打印，密钥脱敏。"""
    src = cfg.ai_sources
    scheme = (cfg.ai_auth_scheme or "").strip()
    return "\n".join([
        "AI 生效配置（密钥已脱敏）",
        "-" * 68,
        f"  provider   : {cfg.ai_provider}    enabled={cfg.ai_enabled}",
        f"  baseURL    : {cfg.ai_base}   [来源 {src.get('baseURL', '内置默认')}]",
        f"  endpoint   : {cfg.chat_url}   [来源 {src.get('endpoint', '推导')}]",
        f"  model      : {cfg.ai_model}   [来源 {src.get('model', '内置默认')}]",
        f"  apiKey     : {mask_secret(cfg.ai_key)}   [来源 {src.get('apiKey', '未配置')}]",
        f"  auth       : {cfg.ai_auth_header}: "
        f"{(scheme + ' ') if scheme else ''}****",
        f"  chatPath   : {cfg.ai_chat_path}",
        f"  parameters : temperature={cfg.ai_temperature}, "
        f"max_tokens={cfg.ai_max_tokens if cfg.ai_max_tokens > 0 else '不限制(不带该字段)'}, "
        f"放宽上限={cfg.ai_max_tokens_cap or '不设顶'}, "
        f"top_p={cfg.ai_top_p}, "
        f"retries={cfg.ai_retries}, batch={cfg.ai_batch}, "
        f"timeout={cfg.ai_timeout}s, json_mode={'on' if cfg.ai_json_mode else 'off'}",
        f"  健壮性    : 整批失败逐篇重试="
        f"{'开启' if cfg.ai_batch_fallback else '关闭'}；"
        f"关闭思考模式={'开启' if cfg.ai_disable_thinking else '关闭'}",
        f"  headers    : {list(cfg.ai_headers) or '无'}",
        f"  extraBody  : {list(cfg.ai_extra_body) or '无'}",
        f"  prompt     : "
        f"{'自定义' if cfg.ai_system_prompt != SYSTEM_PROMPT else '内置默认'} "
        f"({len(cfg.ai_system_prompt)} 字)",
        "-" * 68,
        "上下文（A）与事实约束（B）",
        f"  已有楼层  : {'开启' if cfg.fetch_replies else '关闭'}，最多 "
        f"{cfg.max_floors} 楼 / 每楼 {cfg.floor_chars} 字；"
        f"已解决帖 {'跳过' if cfg.skip_solved else '不跳过'}",
        f"  保守模式  : {'开启' if cfg.conservative else '关闭'}；"
        f"下结论必须引用证据 = {'是' if cfg.require_evidence else '否'}",
        f"  重访已回帖: {'开启' if cfg.recheck_replied else '关闭'}，"
        f"最多 {cfg.recheck_limit} 篇 / 回溯 {cfg.revisit_hours:g}h；"
        f"我的用户名 = {cfg.my_username or '(未配置，仅靠更优解触发)'}",
        f"  水帖/图文  : casual={'允许' if cfg.allow_casual_reply else '禁用'}"
        f"（每次 ≤{cfg.max_casual} 条），"
        f"learn={'允许' if cfg.allow_learn_reply else '禁用'}"
        f"（每次 ≤{cfg.max_learn} 条），"
        f"media={'允许' if cfg.allow_media else '禁用'}"
        f"（白名单 {', '.join(cfg.media_codes) or '空'}）",
        "-" * 68,
        "帖子来源",
        f"  模式      : {'论坛导读（不区分板块，发布时间倒序）' if cfg.use_guide else '按板块 RSS'}",
        f"  导读      : 页数 {cfg.guide_pages}（每页 {GUIDE_PAGE_SIZE} 条），"
        f"最多取最新 {cfg.guide_limit} 篇",
        f"  板块      : {', '.join(DEFAULT_FORUMS.get(f, str(f)) for f in cfg.fids)}"
        f"（每板块各取最新 {cfg.per_forum_limit} 篇，仅板块模式生效）",
        "-" * 68,
        "检索（D）",
        f"  状态      : {'开启' if cfg.search_ready else '关闭'}；"
        f"源 = {', '.join(cfg.search_providers) or '无'}",
        f"  配额      : 最多检索 {cfg.search_max_queries} 篇 / 每篇 "
        f"{cfg.search_max_evidence} 条 / 片段 {cfg.search_snippet_chars} 字 / "
        f"总预算 {cfg.search_total_chars} 字；间隔 {cfg.search_delay}s",
        f"  相关性门槛: {cfg.search_min_relevance:.2f}（只丢明显跑题的；"
        f"其余证据带 relevance 分数交给模型判断）",
        f"  绿联知识库: {KB_SEARCH_URL}  lang={cfg.search_language}  "
        f"取全文={'是' if cfg.kb_fetch_detail else '否'}",
        f"  本地知识库: {cfg.local_kb_dir}",
        f"  联网补查  : 一手资料不足 {cfg.web_fallback_threshold} 条时触发",
        f"  联网源    : Tavily={'已配' if cfg.tavily_key else '未配'}  "
        f"Bing={'已配' if cfg.bing_key else '未配'}  "
        f"百度={'已配' if cfg.baidu_search_key else '未配'}  "
        f"SearXNG={'已配' if cfg.searxng_url else '未配'}  "
        f"自定义={'已配' if cfg.custom_search_url else '未配'}",
        f"  百度端点  : {cfg.baidu_search_url}",
        "-" * 68,
    ])


def test_ai(cfg: Config) -> int:
    """--test-ai：发一条最小请求，只验证地址 / Key / 模型名是否正确。"""
    if not cfg.ai_ready:
        log("未配置 API Key 或自定义认证头，无法测试连通性")
        return 1
    body = {"model": cfg.ai_model,
            "messages": [{"role": "user", "content": "只回复两个字：正常"}],
            "temperature": 0, "max_tokens": 32}
    body.update({k: v for k, v in cfg.ai_extra_body.items()
                 if k not in ("model", "messages", "response_format")})
    log(f"测试连通性：POST {cfg.chat_url}（model={cfg.ai_model}）")
    try:
        r = requests.post(cfg.chat_url, headers=ai_request_headers(cfg),
                          json=body, timeout=cfg.ai_timeout)
        if r.status_code != 200:
            log(f"  失败 HTTP {r.status_code}: {r.text[:300]}")
            return 1
        resp = r.json()
        content, meta = extract_ai_content(resp)
        log(f"  成功，模型回复：{str(content)[:120]}")
        log(f"  finish_reason={meta.get('finish_reason')} "
            f"正文长度={meta.get('content_len')} "
            f"响应长度={meta.get('raw_len')}")
        if not str(content).strip():
            log("  ⚠ 正文为空：若模型是思考型（如 Xing / Qwen3），"
                "请设 AI_DISABLE_THINKING=1 或加大 AI_MAX_TOKENS")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"  请求异常：{exc}")
        return 1


def test_search(cfg: Config, keyword: str) -> int:
    """--test-search：只跑检索，不访问社区、不调模型、不发帖。"""
    if not cfg.search_ready:
        log("检索未启用（UGREEN_SEARCH=0 或 UGREEN_SEARCH_PROVIDERS=none）")
        return 1
    t = Thread(tid="0", title=keyword, content=keyword, forum="诊断")
    kws = extract_keywords(t)
    t.keywords = kws
    queries = build_search_queries(t, kws) or [keyword]
    log(f"检索自测：来源={cfg.search_providers}，关键词={kws or '（未命中词表）'}")
    log(f"构造查询：{queries}")
    retriever = Retriever(cfg, client=None)
    evs = retriever.collect(t, queries)
    print("\n" + "=" * 68)
    print(f"检索结果：{len(evs)} 条证据（HTTP/调用 {retriever.calls} 次）")
    print("=" * 68)
    for i, e in enumerate(evs, 1):
        print(f"{i}. [{e.ref}] {e.title}")
        print(f"   来源：{e.source}  相关度：{e.score:.1f}")
        print(f"   {e.snippet[:300]}")
        print()
    if retriever.errors:
        print("降级记录：" + "；".join(retriever.errors))
    if not evs:
        print("没有拿到证据。保守模式下模型将只能追问或给排查方向。")
    return 0 if evs else 1


def _first_balanced(text: str, opener: str, closer: str) -> str:
    """从 text 里找出第一组配平的 opener/closer，跳过字符串里的括号。"""
    start = text.find(opener)
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _strip_fences(text: str) -> str:
    """去掉 ```json ... ``` 围栏（含围栏夹在正文中间的形态）。"""
    s = re.sub(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n?", "\n", text or "")
    return s.replace("```", "").strip()


def extract_json_payload(text: str):
    """从模型输出里尽最大努力抠出可解析的 JSON。

    实测踩坑：带思考能力的模型（SiliconFlow 的 Xing 系列、Qwen3 等）经常在
    JSON 前面加一段说明、或套一层 ```json 围栏，直接 json.loads 会报
    "Expecting value: line 1 column 1 (char 0)"。这里依次尝试：
      1) 原文直接解析
      2) 去掉围栏后解析
      3) 扫描第一组配平的 { } 或 [ ] 解析
      4) 退而求其次找 "results" 数组
    """
    s = (text or "").strip().lstrip("\ufeff")
    if not s:
        raise json.JSONDecodeError("模型返回空内容", "", 0)
    for cand in (s, _strip_fences(s)):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    for opener, closer in (("{", "}"), ("[", "]")):
        block = _first_balanced(s, opener, closer)
        if block:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                continue
    idx = s.find('"results"')
    if idx >= 0:
        block = _first_balanced(s[idx:], "[", "]")
        if block:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
    raise json.JSONDecodeError("无法从模型输出中提取出 JSON", s, 0)


def extract_ai_content(resp: dict) -> tuple[str, dict]:
    """从 OpenAI 兼容响应里取正文，兼容 content 为空 / 为列表 / 思考模型等形态。"""
    meta: dict = {}
    choices = resp.get("choices") or []
    if not choices:
        return "", {"reason": "响应里没有 choices"}
    ch = choices[0] or {}
    msg = ch.get("message") or {}
    meta["finish_reason"] = ch.get("finish_reason")
    content = msg.get("content")
    if isinstance(content, list):        # 部分网关返回分片数组
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
            else:
                parts.append(str(p))
        content = "".join(parts)
    content = "" if content is None else str(content)
    reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
    if not content.strip() and reasoning.strip():
        # 模型把 token 全花在思考上，正文是空的——这种情况必须让上层看见
        meta["reasoning_only"] = True
        # 只去掉 <think> 标签，保留思考正文（有时思考里已经含 JSON）
        content = re.sub(r"</?think[^>]*>", " ", reasoning, flags=re.I)
    if not content.strip():
        content = str(ch.get("text") or "")
    meta["content_len"] = len(content)
    meta["raw_len"] = len(json.dumps(resp, ensure_ascii=False))
    return content, meta


def _strict_nudge(body: dict) -> dict:
    """重试时在用户消息末尾追加严格的格式要求（不改系统提示词）。"""
    b = dict(body)
    msgs = [dict(m) for m in b.get("messages", [])]
    for m in reversed(msgs):
        if m.get("role") == "user":
            m["content"] = (str(m.get("content") or "") + "\n\n"
                            "【严格格式要求】上一次输出无法被解析为 JSON。"
                            "这次只输出一个 JSON 对象，形如 {\"results\":[{\"tid\":\"...\","
                            "\"should_reply\":true,\"kind\":\"text\",\"reply\":\"...\"}]}，"
                            "不要任何解释文字、不要 Markdown 围栏、不要前后缀。")
            break
    b["messages"] = msgs
    return b


def parse_ai_response(text: str, valid_tids: set[str]) -> dict[str, dict]:
    data = extract_json_payload(text)
    if isinstance(data, dict):
        rows = data.get("results") or data.get("replies") or ([data] if data.get("tid") else [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("tid", "")).strip()
        if tid not in valid_tids:
            continue
        raw_kind = str(row.get("kind") or "text").strip().lower()
        kind = raw_kind if raw_kind in ("emoji", "casual", "learn") else "text"
        confidence = str(row.get("confidence") or "low").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        answer_mode = str(row.get("answer_mode") or "probe").strip().lower()
        if answer_mode not in ("direct", "probe", "emoji", "casual", "learn"):
            answer_mode = "probe"
        # 形态与模式对齐：表情 / 水帖 / 学习点赞不该自称在下技术结论
        if kind in ("emoji", "casual", "learn"):
            answer_mode = kind if kind != "emoji" else "emoji"
            confidence = "low"
        # 模型可能给自己贴金：没有 high 证据就不允许直接下结论
        if answer_mode == "direct" and confidence != "high":
            answer_mode = "probe"
        raw_used = row.get("evidence_used") or []
        if isinstance(raw_used, str):
            raw_used = [raw_used]
        used = [str(x).strip() for x in raw_used if str(x).strip()][:6]
        fmt = str(row.get("format") or "plain").strip().lower()
        if fmt not in ("plain", "steps"):
            fmt = "plain"
        out[tid] = {
            "tid": tid,
            "should_reply": bool(row.get("should_reply")),
            "kind": kind,
            "reply": str(row.get("reply") or "").strip(),
            "reason": str(row.get("reason") or "").strip(),
            "confidence": confidence,
            "answer_mode": answer_mode,
            "evidence_used": used,
            "format": fmt,
            "media": str(row.get("media") or "").strip()[:40],
        }
    return out


# ===== forum/revisit =====
# 绿联 NAS 社区自动回复 —— 重访：已回复帖的二次触达。








def pick_revisit_candidates(state: dict, cfg: Config,
                            now: datetime) -> list[Thread]:
    """从状态文件里挑出「最近回复过、值得回头看看」的帖子。

    需求：回复过的帖子尽量不要再回复，除非被别人点名，或出现了更优的解法。
    这里只负责选出候选；真正要不要再回，看楼层内容（detect_revisit_trigger）。
    """
    replied_map = state.get("replied") or {}
    cutoff = now - timedelta(hours=cfg.revisit_hours)
    picked: list[Thread] = []

    def _at(rec) -> str:
        return rec.get("at", "") if isinstance(rec, dict) else ""

    # 最近回复过的优先
    items = sorted(replied_map.items(), key=lambda kv: _at(kv[1]), reverse=True)
    for tid, rec in items:
        if len(picked) >= cfg.recheck_limit:
            break
        if not isinstance(rec, dict):
            continue
        try:
            at = datetime.strptime(_at(rec), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if at < cutoff:
            continue
        forum = rec.get("forum", "")
        t = Thread(tid=str(tid), title=rec.get("title", ""), forum=forum,
                   url=THREAD_URL.format(tid=tid),
                   published=at,          # 报告里显示为「上次回复时间」
                   my_previous_reply=rec.get("reply", ""))
        t.fid = next((f for f, n in DEFAULT_FORUMS.items() if n == forum), 0)
        picked.append(t)
    return picked


def detect_revisit_trigger(t: Thread, cfg: Config) -> str:
    """判断已回复过的帖子现在是否值得再回一次，返回触发原因（空串=不回）。

    两种触发：
      1. 有人点名我——需要配置 UGREEN_MY_USERNAME，否则这一路不生效；
      2. 新楼层里出现了更优的解法。
    Discuz 楼层按时间正序排列，末尾几楼最新，所以只看尾部若干层。
    """
    if not t.replies:
        return ""
    me = (cfg.my_username or "").strip()
    for r in t.replies[-5:]:
        text = r.text or ""
        if me and (f"@{me}" in text or me in text):
            return f"{r.author or '有人'} 点名了你（@{me}）"
        hit = next((k for k in BETTER_SOLUTION_MARKERS if k in text), None)
        if hit:
            return f"{r.author or '有人'} 提到了更优解法（“{hit}”）"
    return ""


# ===== forum/state =====
# 绿联 NAS 社区自动回复 —— 状态持久化与每日配额。








def is_blocked(t: Thread, cfg: Config, replied: set[str]) -> str:
    if t.tid in replied:
        return "已回复过"
    if t.tid in BLOCK_TIDS:
        return "官方制度帖（黑名单 tid）"
    if any(k in t.title for k in BLOCK_TITLE_KEYWORDS):
        return "官方制度帖（标题关键词）"
    if cfg.skip_events and t.fid == 83:
        return "已设置跳过社区活动帖"
    if len(t.title) < 4:
        return "标题过短或抓不到标题"
    return ""


def load_state(cfg: Config) -> dict:
    try:
        if cfg.state_file.exists():
            return json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log("状态文件损坏，按首次运行处理")
    return {}


DAILY_KEEP_DAYS = 30


def daily_used(state: dict, day: str) -> int:
    """今天（自然日，服务器本地时间）已经实发成功多少条回复。

    计数只统计「发送成功」的回复；失败的请求不占配额。
    """
    daily = state.get("daily")
    if not isinstance(daily, dict):
        return 0
    try:
        return max(0, int(daily.get(day, 0)))
    except (TypeError, ValueError):
        return 0


def bump_daily_used(state: dict, day: str, n: int = 1) -> int:
    """把今天的发送计数 +n，并顺手清掉太老的日期，避免状态文件无限膨胀。

    返回更新后的今日计数。
    """
    daily = state.get("daily")
    if not isinstance(daily, dict):
        daily = {}
    try:
        cur = max(0, int(daily.get(day, 0)))
    except (TypeError, ValueError):
        cur = 0
    daily[day] = cur + max(0, n)
    # 日期是 YYYY-MM-DD，字典序即时间序，保留最近 DAILY_KEEP_DAYS 天
    state["daily"] = {k: daily[k] for k in sorted(daily)[-DAILY_KEEP_DAYS:]}
    return daily[day]


def save_state(cfg: Config, state: dict, now: datetime,
               quiet: bool = False, update_last_run: bool = True) -> None:
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        if update_last_run:
            state["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
        state["recent_replies"] = state.get("recent_replies", [])[-50:]
        cfg.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        if not quiet:
            log(f"状态已写入 {cfg.state_file}")
    except Exception as exc:  # noqa: BLE001
        log(f"状态写入失败：{exc}")


# ===== runtime/notify =====
# 绿联 NAS 社区自动回复 —— 推送渠道：sendNotify / 群机器人 / 应用消息。






def notify_via_sendnotify(title: str, content: str) -> bool:
    candidates: list[Path] = []
    ql_dir = env_str("QL_DIR")
    if ql_dir:
        candidates += [Path(ql_dir) / "sendNotify.py",
                       Path(ql_dir) / "scripts" / "sendNotify.py"]
    candidates += [Path("/ql/sendNotify.py"), Path("/ql/scripts/sendNotify.py"),
                   SCRIPT_DIR / "sendNotify.py"]
    for p in candidates:
        if not p.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_ql_sendnotify", str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "send", None)
            if not fn:
                continue
            try:
                fn(title, content)
            except TypeError:
                fn(title, content, "")
            log("已通过青龙 sendNotify 推送")
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"sendNotify 调用失败（{p}）：{exc}")
    return False


def notify_via_qywx_key(title: str, content: str) -> bool:
    key = env_str("QYWX_KEY")
    if not key:
        return False
    try:
        r = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
            json={"msgtype": "text",
                  "text": {"content": truncate_bytes(f"{title}\n\n{content}", 1900)}},
            timeout=20)
        ok = r.json().get("errcode") == 0
        log("企业微信机器人推送" + ("成功" if ok else f"失败：{r.text[:160]}"))
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"企业微信机器人推送异常：{exc}")
        return False


def notify_via_qywx_am(title: str, content: str) -> bool:
    """青龙 QYWX_AM 标准格式：corpid,corpsecret,touser,agentid（可含第 5 段）。"""
    am = env_str("QYWX_AM")
    if not am:
        return False
    parts = [x.strip() for x in am.split(",")]
    if len(parts) < 4:
        return False
    corpid, secret, touser, agentid = parts[0], parts[1], parts[2], parts[3]
    # 兼容部分脚本写反了的顺序：agentid 通常是纯数字，touser 常为 @all / 账号名
    if agentid.isdigit() and not touser.isdigit():
        touser, agentid = agentid, touser
    try:
        tr = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                          params={"corpid": corpid, "corpsecret": secret},
                          timeout=20).json()
        token = tr.get("access_token")
        if not token:
            return False
        r = requests.post(
            "https://qyapi.weixin.qq.com/cgi-bin/message/send",
            params={"access_token": token}, timeout=20,
            json={"touser": touser or "@all", "msgtype": "text",
                  "agentid": agentid,
                  "text": {"content": truncate_bytes(f"{title}\n\n{content}", 1900)}})
        ok = r.json().get("errcode") == 0
        log("企业微信应用消息推送" + ("成功" if ok else f"失败：{r.text[:160]}"))
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"企业微信应用消息推送异常：{exc}")
        return False


def send_notification(title: str, content: str) -> None:
    if notify_via_sendnotify(title, content):
        return
    if notify_via_qywx_key(title, content):
        return
    if notify_via_qywx_am(title, content):
        return
    log("未找到可用推送渠道（sendNotify / QYWX_KEY / QYWX_AM），仅打印日志")


# ===== runtime/report =====
# 绿联 NAS 社区自动回复 —— 运行报告渲染。









def render_report(results: list[dict], cfg: Config,
                  since: datetime, now: datetime,
                  candidates: int, ai_mode: str,
                  run_limit: Optional[int] = None,
                  used_today: int = 0) -> str:
    if cfg.use_guide:
        source_line = (f"来源：论坛导读（不区分板块，发布时间倒序，"
                       f"取最新 ≤{cfg.guide_limit} 篇）")
    else:
        source_line = (f"板块：{', '.join(DEFAULT_FORUMS.get(f, str(f)) for f in cfg.fids)}"
                       f"（每板块各取最新 {cfg.per_forum_limit} 篇）")
    limit = cfg.max_replies if run_limit is None else run_limit
    L = ["=" * 68,
         "绿联 NAS 社区 · AI 自动回复",
         f"窗口：{since:%m-%d %H:%M} ~ {now:%m-%d %H:%M}   "
         f"模式：{'演练（不发帖）' if cfg.dry_run else '实发'}   AI：{ai_mode}",
         source_line,
         f"候选 {candidates} 篇 / 处理 {len(results)} 篇 / 上限 {limit} 条回复"
         f" / 表情回复 {'允许' if cfg.allow_emoji_reply else '禁用'}",
         (f"每日配额：{used_today}/{cfg.daily_quota} 条"
          f"（{'演练不占用' if cfg.dry_run else '实发成功才计数'}）")
         if cfg.daily_quota > 0 else "每日配额：不限制",
         f"上下文：已有楼层 ≤{cfg.max_floors} 楼 / 检索 "
         f"{'开' if cfg.search_ready else '关'}"
         f"（{', '.join(cfg.search_providers) or '无'}） / 保守模式 "
         f"{'开' if cfg.conservative else '关'}",
         f"重访已回帖 {'开' if cfg.recheck_replied else '关'}"
         f"（≤{cfg.recheck_limit} 篇 / {cfg.revisit_hours:g}h） / "
         f"水帖 {'开' if cfg.allow_casual_reply else '关'}（≤{cfg.max_casual}） / "
         f"学习点赞 {'开' if cfg.allow_learn_reply else '关'}（≤{cfg.max_learn}） / "
         f"图文 {'开' if cfg.allow_media else '关'}",
         "=" * 68, ""]
    if not results:
        L.append("本次没有需要处理的帖子。")
    for i, r in enumerate(results, 1):
        t: Thread = r["thread"]
        L.append(f"{i}. [{t.forum}] {t.title}")
        L.append(f"   {t.when} | {t.author} | {t.url}")
        if r.get("evidence"):
            L.append(f"   [检索证据] {describe_evidence(r['evidence'])}")
        if r.get("confidence"):
            used = ", ".join(r.get("evidence_used") or []) or "无"
            L.append(f"   [依据] {r.get('answer_mode', '?')}/"
                     f"{r.get('confidence')}，引用 {used}")
        if r.get("revisit"):
            L.append(f"   [重访] {r.get('revisit_reason', '')}")
        if r.get("skip"):
            L.append(f"   [跳过] {r['skip']}")
            if r.get("reply"):
                L.append(f"   [被拦下的回复] {r['reply']}")
            L.append("")
            continue
        if r.get("floors"):
            L.append(f"   [已有楼层] {r['floors']} 条")
        if r.get("ai_reason"):
            L.append(f"   [AI 判断] {r['ai_reason']}")
        tag = ""
        if r.get("casual"):
            tag = " · 水帖"
        elif r.get("learn"):
            tag = " · 学习点赞"
        elif r.get("emoji"):
            tag = " · 表情"
        if r.get("media"):
            tag += f" · 图文{r['media']}"
        if r.get("format") == "steps":
            tag += " · 分步"
        L.append(f"   [回复{tag}] {r.get('reply', '')}")
        L.append(f"   [结果] {r.get('outcome', '')}")
        L.append("")
    skipped = sum(1 for r in results if r.get("skip"))
    failed = sum(1 for r in results if str(r.get("outcome", "")).startswith("发送失败"))
    L.append("-" * 68)
    if cfg.dry_run:
        L.append(f"合计：拟回复 "
                 f"{sum(1 for r in results if r.get('reply') and not r.get('skip'))} 篇 / "
                 f"跳过 {skipped} 篇（演练，均未发送）")
    else:
        L.append(f"合计：成功 {sum(1 for r in results if r.get('ok'))} / "
                 f"跳过 {skipped} / 失败 {failed}")
    if cfg.dry_run:
        L.append("当前为演练模式：以上回复均未发送。设置 UGREEN_DRY_RUN=0 后才会真正发帖。")
    return "\n".join(L)


def render_notify(results: list[dict], cfg: Config) -> str:
    mode = "演练" if cfg.dry_run else "实发"
    n = (sum(1 for r in results if r.get("reply") and not r.get("skip")) if cfg.dry_run
         else sum(1 for r in results if r.get("ok")))
    L = [f"绿联社区 AI 回复 · {mode}",
         f"处理 {len(results)} 篇，{'拟回复' if cfg.dry_run else '成功'} {n} 条", ""]
    for r in results:
        if not r.get("reply") or r.get("skip"):
            continue
        t: Thread = r["thread"]
        L.append(f"· [{t.forum}] {clip_text(t.title, 48)}")
        L.append(f"  {clip_text(r['reply'], 90)}")
        L.append(f"  {r.get('outcome', '')}")
        L.append(f"  {t.url}")
    return truncate_bytes("\n".join(L), 1900)


# ===== runtime/cli =====
# 绿联 NAS 社区自动回复 —— 命令行入口与主流程。


















def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="绿联 NAS 社区 · 六板块 AI 自动回复")
    ap.add_argument("--hours", type=float, default=None, help="只看最近 N 小时的新帖")
    ap.add_argument("--limit", type=int, default=None, help="本次最多发几条回复")
    ap.add_argument("--daily-quota", type=int, default=None,
                    help="每天最多发几条回复（默认 10，0 = 不限制）")
    ap.add_argument("--per-forum", type=int, default=None,
                    help="每个板块各取最新几篇（默认 4）")
    ap.add_argument("--detail-limit", type=int, default=None, help="最多抓多少篇详情")
    ap.add_argument("--no-emoji", action="store_true", help="禁用表情回复")
    ap.add_argument("--fids", default="", help="覆盖板块，如 10,9,13,14,83,12")
    ap.add_argument("--use-guide", dest="use_guide", action="store_true",
                    default=None, help="按论坛导读抓帖（不区分板块，发布时间倒序）")
    ap.add_argument("--forums", dest="use_guide", action="store_false",
                    help="改回按 UGREEN_FIDS 里的板块 RSS 抓帖")
    ap.add_argument("--guide-pages", type=int, default=None,
                    help="导读抓几页（每页 50 条），默认 1")
    ap.add_argument("--guide-limit", type=int, default=None,
                    help="导读模式下最多取最新多少篇，默认 20")
    ap.add_argument("--model", default="", help="覆盖 AI_MODEL")
    ap.add_argument("--base-url", default="", help="覆盖 AI_BASE_URL")
    ap.add_argument("--api-key", default="", help="覆盖 AI_API_KEY")
    ap.add_argument("--endpoint", default="", help="直接指定完整 chat/completions 地址")
    ap.add_argument("--show-config", action="store_true",
                    help="打印生效的 AI 配置（密钥脱敏）后退出，不发任何请求")
    ap.add_argument("--test-ai", action="store_true",
                    help="发一条最小请求测试模型连通性后退出")
    ap.add_argument("--test-search", default="", metavar="关键词",
                    help="只跑检索并打印证据，不访问社区 / 不调模型 / 不发帖")
    ap.add_argument("--search-providers", default="",
                    help="覆盖检索源，如 ugnas-kb,local,tavily；none 可关闭")
    ap.add_argument("--no-search", action="store_true", help="本次关闭检索")
    ap.add_argument("--no-replies", action="store_true", help="不抓取已有楼层回复")
    ap.add_argument("--no-conservative", action="store_true",
                    help="关闭保守模式（不建议，可能发出无据结论）")
    ap.add_argument("--dry-run", action="store_true", help="强制演练（默认）")
    ap.add_argument("--live", action="store_true", help="真正发送回复")
    ap.add_argument("--mark", action="store_true", help="演练时也更新状态水位")
    ap.add_argument("--no-mark", action="store_true", help="不写状态文件")
    ap.add_argument("--no-notify", action="store_true", help="不推送")
    ap.add_argument("--debug", action="store_true", help="打印回复响应体片段")
    return ap


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args()
    try:
        cfg = Config.from_env(args)
    except ValueError as exc:
        log(f"AI 配置错误：{exc}")
        return 1

    # 两个诊断命令都不发社区请求、不发帖
    if args.show_config:
        print(describe_ai_config(cfg))
        return 0
    if args.test_ai:
        return test_ai(cfg)
    if args.test_search.strip():
        return test_search(cfg, args.test_search.strip())

    now = datetime.now()
    state = load_state(cfg)
    replied = set((state.get("replied") or {}).keys())
    recent = list(state.get("recent_replies") or [])

    today = now.strftime("%Y-%m-%d")
    used_today = daily_used(state, today)
    if cfg.daily_quota > 0:
        quota_left = max(0, cfg.daily_quota - used_today)
        # 本次实发上限 = 单次上限 与 今日剩余配额 取小
        run_limit = min(cfg.max_replies, quota_left)
    else:
        quota_left = -1          # -1 表示不限制
        run_limit = cfg.max_replies
    if cfg.dry_run:
        # 演练不发帖，既不消耗配额也不受配额限制
        run_limit = cfg.max_replies
        quota_exhausted = False
    else:
        quota_exhausted = cfg.daily_quota > 0 and quota_left == 0

    if cfg.use_guide:
        source_desc = (f"来源=论坛导读（不区分板块，发布时间倒序，"
                       f"≤{cfg.guide_limit} 篇 / {cfg.guide_pages} 页）")
    else:
        source_desc = (f"来源=板块 RSS，板块={cfg.fids}，"
                       f"每板块最新 {cfg.per_forum_limit} 篇")
    log(f"模式={'演练' if cfg.dry_run else '实发'}，{source_desc}，"
        f"回复上限={cfg.max_replies}，"
        f"表情回复={'允许' if cfg.allow_emoji_reply else '禁用'}，"
        f"AI={'已启用' if cfg.use_ai else '未启用'}")
    if cfg.daily_quota > 0:
        log(f"每日配额 {cfg.daily_quota} 条：今日已用 {used_today} 条，"
            f"剩余 {quota_left} 条"
            + (f"，本次最多发 {run_limit} 条" if not cfg.dry_run else "（演练不占用配额）"))
        if quota_exhausted:
            log(f"今日配额已用满（{used_today}/{cfg.daily_quota}），"
                f"本次不再抓帖、不再发帖")
        if not cfg.dry_run and not cfg.mark_state:
            log("注意：本次禁用了写状态文件（--no-mark / UGREEN_MARK_STATE=0），"
                "每日配额无法累计，配额不会生效")
    else:
        log("每日配额：不限制（UGREEN_DAILY_QUOTA=0）")

    # 配额用满：整轮跳过，连帖子都不抓，避免无谓请求
    if quota_exhausted:
        if cfg.notify:
            send_notification(
                f"绿联社区 AI 回复：今日配额已满",
                f"今日已发 {used_today}/{cfg.daily_quota} 条，本次未执行。\n"
                f"可调 UGREEN_DAILY_QUOTA 调整上限，或等下一个自然日。")
        if cfg.mark_state:
            save_state(cfg, state, now)
        return 0
    log(f"上下文：已有楼层={'抓取' if cfg.fetch_replies else '不抓'}"
        f"（≤{cfg.max_floors} 楼），"
        f"检索={'开启' if cfg.search_ready else '关闭'}"
        f"（{', '.join(cfg.search_providers) or '无'}，≤{cfg.search_max_queries} 篇），"
        f"保守模式={'开启' if cfg.conservative else '关闭'}")
    log(f"回复策略：水帖{'允许' if cfg.allow_casual_reply else '禁用'}"
        f"（≤{cfg.max_casual} 条/次），"
        f"图文{'允许' if cfg.allow_media else '禁用'}，"
        f"重访已回帖{'开启' if cfg.recheck_replied else '关闭'}"
        f"（≤{cfg.recheck_limit} 篇 / {cfg.revisit_hours:g}h 内）")
    if cfg.use_ai:
        log(f"AI 接入：{cfg.ai_model} @ {cfg.ai_base} → {cfg.chat_url}，"
            f"Key={mask_secret(cfg.ai_key)}"
            f"（来自 {cfg.ai_sources.get('apiKey', '自定义认证头')}）")
        log(f"AI 参数：max_tokens="
            f"{'不限制（不带该字段）' if cfg.ai_max_tokens <= 0 else cfg.ai_max_tokens}，"
            f"batch={cfg.ai_batch}，思考模式="
            f"{'关闭' if cfg.ai_disable_thinking else '开启'}，"
            f"json_mode={'开' if cfg.ai_json_mode else '关'}")

    if not cfg.dry_run:
        if not cfg.cookie:
            log("实发模式必须配置 UGREEN_COOKIE（浏览器登录后 F12 复制完整 Cookie）")
            return 1
        if not cfg.use_ai:
            log("实发模式必须启用 AI 并配置密钥（AI_ENABLED=1，且 "
                "AI_API_KEY / OPENAI_API_KEY / SILICONFLOW_API_KEY 等任一有值）："
                "本地模板与帖子内容无关，为避免变成灌水，已禁止无 AI 发帖"
                "（演练模式不受限制）")
            return 1

    client = UgnasClient(cfg)

    if not cfg.dry_run and not client.check_login():
        log("Cookie 无效或已过期（页面仍是游客态），终止，避免拿游客 formhash 空跑")
        return 1

    since = now - timedelta(hours=cfg.hours)
    if args.hours is None and state.get("last_run") and not cfg.dry_run:
        try:
            since = datetime.strptime(state["last_run"], "%Y-%m-%d %H:%M:%S")
            log(f"沿用上次水位 {since:%Y-%m-%d %H:%M:%S}")
        except ValueError:
            pass
    if since > now:
        since = now - timedelta(hours=cfg.hours)

    if cfg.use_guide:
        log("抓取论坛导读（不区分板块，按发布时间倒序）...")
        pool = client.fetch_guide()
        log(f"导读汇总 {len(pool)} 篇（去重后）")
    else:
        log("抓取各板块 RSS ...")
        pool = client.fetch_rss()
        log(f"RSS 汇总 {len(pool)} 篇（去重后）")
    if not pool:
        log("没有抓到任何帖子（RSS/导读不可用或网络异常），本次结束")
        return 0

    # 时间窗口 + 黑名单 + 已回复去重
    fresh: list[Thread] = []
    for t in pool:
        if t.published and not (since < t.published <= now + timedelta(minutes=5)):
            continue
        reason = is_blocked(t, cfg, replied)
        if reason:
            t.skip_reason = reason
            continue
        fresh.append(t)
    log(f"窗口内候选 {len(fresh)} 篇（过滤掉旧帖 / 制度帖 / 已回复）")
    if cfg.use_guide:
        # 导读本身已是全局时间倒序，直接取最新的 N 篇即可
        fresh.sort(key=lambda x: x.published or datetime.min, reverse=True)
        if fresh:
            log(f"  最新：{clip_text(fresh[0].title, cfg.log_title_chars)}")
        fresh = fresh[:cfg.guide_limit]
        log(f"按导读取最新 {cfg.guide_limit} 篇，共 {len(fresh)} 篇")
    else:
        fresh = select_per_forum_latest(fresh, cfg.per_forum_limit,
                                        cfg.log_title_chars)
        log(f"按板块各取最新 {cfg.per_forum_limit} 篇并交错后，共 {len(fresh)} 篇")

    # 已回复过的帖子默认不再回复；这里挑出「最近回过、值得回头看看」的候选，
    # 真正要不要再回，等抓到楼层内容后再按触发条件判定。
    revisit_pool: list[Thread] = []
    if cfg.recheck_replied and cfg.recheck_limit > 0:
        revisit_pool = pick_revisit_candidates(state, cfg, now)
        if revisit_pool:
            log(f"重访候选 {len(revisit_pool)} 篇（最近 {cfg.revisit_hours:g}h 内回复过）")

    results: list[dict] = []
    # 超出处理上限的也记录一条跳过原因，报告里能看到全貌
    for t in fresh[cfg.max_detail:]:
        results.append({"thread": t, "skip": "超出本次详情抓取上限"})
    todo = fresh[:cfg.max_detail]

    log(f"抓取详情页 {len(todo)} 篇（标题 + 正文 + formhash + 回复地址）")
    for i, t in enumerate(todo, 1):
        client.fetch_detail(t)
        if i % 5 == 0 or i == len(todo):
            log(f"  进度 {i}/{len(todo)}")
        if cfg.request_delay:
            time.sleep(cfg.request_delay)

    for t in todo:
        if t.skip_reason:
            results.append({"thread": t, "skip": t.skip_reason})
        elif not t.can_reply and not cfg.dry_run:
            results.append({"thread": t, "skip": "页面未提供可用的回复表单（可能已锁帖）"})

    # 重访：抓详情页看有没有「被点名」或「更优解法」这类新触发条件
    revisit_ok: list[Thread] = []
    for t in revisit_pool:
        client.fetch_detail(t, floors=max(cfg.max_floors, 15))
        trigger = detect_revisit_trigger(t, cfg)
        if trigger and not t.skip_reason:
            t.revisit = True
            t.revisit_reason = trigger
            revisit_ok.append(t)
            log(f"  重访命中 tid={t.tid}：{trigger}")
        else:
            t.skip_reason = t.skip_reason or "已回复过，且没有出现新的触发条件"
        if cfg.request_delay:
            time.sleep(cfg.request_delay)
    if revisit_pool:
        log(f"重访检查完成：{len(revisit_ok)}/{len(revisit_pool)} 篇值得再回")

    active = [t for t in todo if not t.skip_reason and (cfg.dry_run or t.can_reply)]
    active += [t for t in revisit_ok if not t.skip_reason and (cfg.dry_run or t.can_reply)]
    for t in revisit_pool:
        if not t.revisit:
            results.append({"thread": t, "skip": t.skip_reason})
    log(f"进入 AI 判断 {len(active)} 篇"
        + (f"（已抓到楼层共 {sum(len(t.replies) for t in active)} 条）"
           if cfg.fetch_replies else ""))

    # D：在调模型之前把资料检索好，注入每篇帖子的 evidence 字段
    retriever: Optional[Retriever] = None
    if cfg.search_ready and cfg.search_max_queries > 0 and active:
        retriever = Retriever(cfg, client)
        scope = active[:cfg.search_max_queries]
        log(f"检索证据 {len(scope)} 篇（来源：{', '.join(cfg.search_providers)}）")
        for i, t in enumerate(scope, 1):
            t.keywords = extract_keywords(t)
            queries = build_search_queries(t, t.keywords)
            if queries:
                t.evidence = retriever.collect(t, queries)
            log(f"  [{i}/{len(scope)}] tid={t.tid} "
                f"关键词={'/'.join(t.keywords[:3]) or '无'} → "
                f"证据 {len(t.evidence)} 条（{t.search_note}）")
        if retriever.errors:
            log(f"  检索降级 {len(retriever.errors)} 次，首条：{retriever.errors[0]}")
    elif not cfg.search_ready:
        log("检索未启用：保守模式下模型只能追问或给排查方向，不得下结论")

    # AI 批量判断与生成
    decisions: dict[str, dict] = {}
    ai_mode = "未启用"
    if cfg.use_ai:
        ai_mode = f"{cfg.ai_model} @ {cfg.ai_base}"
        batches = [active[i:i + cfg.ai_batch]
                   for i in range(0, len(active), cfg.ai_batch)]
        for bi, batch in enumerate(batches, 1):
            log(f"  模型批次 {bi}/{len(batches)}（{len(batch)} 篇）")
            got = call_ai(batch, cfg)
            if got:
                decisions.update(got)
            missing = [t for t in batch if t.tid not in decisions]
            if missing and cfg.ai_batch_fallback:
                # 整批解析失败时逐篇重试，避免 5 篇帖子被静默丢掉
                log(f"  批次 {bi} 未覆盖 {len(missing)} 篇，逐篇重试")
                for t in missing:
                    one = call_ai([t], cfg)
                    if one:
                        decisions.update(one)
            if bi < len(batches):
                time.sleep(1)
    elif cfg.dry_run and cfg.local_fallback:
        ai_mode = "本地模板演示（未配置 AI Key，仅限演练）"
        for t in active:
            ok, reply, kind = local_fallback_reply(t)
            decisions[t.tid] = {"should_reply": ok, "reply": reply,
                                "kind": kind,
                                "reason": "本地关键词兜底" if ok else "无 AI Key 且未命中关键词"}
    else:
        ai_mode = "演练演示已关闭（UGREEN_LOCAL_FALLBACK=0）"

    sent = 0
    casual_used = 0
    learn_used = 0
    for t in active:
        d = decisions.get(t.tid)
        if not d:
            results.append({"thread": t, "skip": "模型未返回该帖结果"})
            continue
        if not d.get("should_reply"):
            # 分享帖 / 自荐帖：模型常以「没什么可补充」「楼主已自行说明」为由跳过，
            # 但按规则这类帖子应该回一句学习/点赞。软理由则补一条，硬理由则尊重跳过。
            share = is_share_post(t)
            reason = str(d.get("reason", ""))
            hard = any(h in reason for h in HARD_SKIP_REASONS)
            if (share and not hard and cfg.allow_learn_reply
                    and learn_used < cfg.max_learn and not already_praised(t)):
                d = {"should_reply": True, "kind": "learn",
                     "reply": share_learn_reply(t), "reason": "分享帖按规则回一条学习/点赞",
                     "confidence": "low", "answer_mode": "learn",
                     "evidence_used": [], "format": "plain", "media": ""}
            else:
                results.append({"thread": t, "skip": f"模型判定不值得回复：{reason}",
                                "share": share})
                continue
        kind = d.get("kind", "text")
        share = is_share_post(t)
        if kind == "casual":
            if not cfg.allow_casual_reply:
                results.append({"thread": t, "skip": "水帖回复已被配置禁用"})
                continue
            if casual_used >= cfg.max_casual:
                results.append({"thread": t, "skip": f"水帖回复已达本次上限（{cfg.max_casual} 条）"})
                continue
        if kind == "learn":
            if not cfg.allow_learn_reply:
                results.append({"thread": t, "skip": "学习/点赞型回复已被配置禁用"})
                continue
            if learn_used >= cfg.max_learn:
                results.append({"thread": t, "skip": f"学习/点赞回复已达本次上限（{cfg.max_learn} 条）"})
                continue
        # 图文代码只从白名单里取，模型塞其它东西一律忽略
        media = d.get("media", "") if cfg.allow_media else ""
        ok, reply = sanitize_reply(d.get("reply", ""), recent,
                                   kind=kind, allow_emoji=cfg.allow_emoji_reply,
                                   media=media, media_codes=cfg.media_codes,
                                   allow_casual=cfg.allow_casual_reply,
                                   share_post=share)
        if not ok:
            results.append({"thread": t, "skip": f"回复未通过质检：{reply}"})
            continue
        # B：保守模式硬闸，拦住没有证据支撑的确定性结论
        ok, why = validate_claim(reply, d, t, cfg)
        if not ok:
            results.append({"thread": t, "skip": f"保守模式拦截：{why}",
                            "reply": reply, "evidence": t.evidence,
                            "confidence": d.get("confidence", ""),
                            "answer_mode": d.get("answer_mode", "")})
            continue
        is_emoji = kind == "emoji" or is_emoji_reply(reply)
        is_casual = kind == "casual"
        is_learn = kind == "learn"
        meta = {
            "evidence": t.evidence,
            "confidence": d.get("confidence", ""),
            "answer_mode": d.get("answer_mode", ""),
            "evidence_used": d.get("evidence_used", []),
            "floors": len(t.replies),
            "casual": is_casual,
            "learn": is_learn,
            "share": share,
            "media": media,
            "format": d.get("format", "plain"),
            "revisit": t.revisit,
            "revisit_reason": t.revisit_reason,
        }
        if sent >= run_limit:
            results.append({"thread": t, "skip": "已达本次回复上限",
                            "reply": reply, "emoji": is_emoji, **meta})
            continue

        if cfg.dry_run:
            results.append({"thread": t, "reply": reply, "outcome": "演练：未发送",
                            "ai_reason": d.get("reason", ""), "ok": False,
                            "emoji": is_emoji, **meta})
            # 演练时也累加去重池，避免报告里出现两句一模一样的模板
            recent.append(reply)
            sent += 1
            if is_casual:
                casual_used += 1
            if is_learn:
                learn_used += 1
            continue

        posted, why, body = client.post_reply(t, reply)
        if cfg.debug and body:
            log(f"  响应体片段 tid={t.tid}: {body[:300]}")
        results.append({"thread": t, "reply": reply,
                        "outcome": ("发送成功" if posted else f"发送失败：{why}"),
                        "ai_reason": d.get("reason", ""), "ok": posted,
                        "emoji": is_emoji, **meta})
        # 打印每条的发帖结果：否则实发时只看到「等待 N 秒」，
        # 不知道刚才到底发了哪一条
        mark = "✓" if posted else "✗"
        snippet = clip_text(reply.replace("\n", " "), 40)
        if posted:
            log(f"  {mark} 已回复 tid={t.tid} [{t.forum}] {t.title} → {snippet}")
        else:
            log(f"  {mark} 发送失败 tid={t.tid} [{t.forum}] {t.title}"
                f" → {why}")
        if posted:
            sent += 1
            if is_casual:
                casual_used += 1
            if is_learn:
                learn_used += 1
            state.setdefault("replied", {})[t.tid] = {
                "at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "forum": t.forum, "title": t.title, "reply": reply,
                "revisit": bool(t.revisit),
            }
            recent.append(reply)
            # 每日配额只统计实发成功的回复
            used_today = bump_daily_used(state, today)
            if cfg.daily_quota > 0:
                log(f"  今日配额 {used_today}/{cfg.daily_quota} 条")
            # 每发成功一条就立刻落盘：中途被青龙超时杀掉也不丢配额和已回复记录
            if cfg.mark_state:
                save_state(cfg, state, now, quiet=True, update_last_run=False)
            if sent < run_limit:
                # 随机间隔，模拟真人节奏、降低被风控的概率
                delay = random.randint(cfg.min_delay, cfg.max_delay)
                left = run_limit - sent
                log(f"  等待 {delay} 秒后继续 "
                    f"（回复间隔随机，剩余名额 {left} 条）...")
                time.sleep(delay)

    report = render_report(results, cfg, since, now, len(fresh), ai_mode,
                           run_limit=run_limit, used_today=used_today)
    print("\n" + report + "\n")

    if cfg.notify:
        n = (sum(1 for r in results if r.get("reply") and not r.get("skip")) if cfg.dry_run
             else sum(1 for r in results if r.get("ok")))
        send_notification(
            f"绿联社区 AI 回复：{'拟' if cfg.dry_run else '成功'}{n} 条"
            + ("（演练）" if cfg.dry_run else ""),
            render_notify(results, cfg))

    if cfg.mark_state:
        if not cfg.dry_run:
            state["recent_replies"] = recent[-50:]
        # 只保留最近 3000 条已回复记录，避免状态文件无限膨胀
        items = sorted((state.get("replied") or {}).items(),
                       key=lambda kv: kv[1].get("at", ""))
        state["replied"] = dict(items[-3000:])
        save_state(cfg, state, now)

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
