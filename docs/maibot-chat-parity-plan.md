# 小豆聊天补全：MaiBot 核心与本机插件对照计划

对照基线：2026-11-26。**2026-11-27 已定范围：实施 01–22、24–28，共 27 项；23 不做。** 属于 Phase 3 的聊天补完，以 3.6（QQ）、3.7（行为回放）、3.14（表情）和 3.15（规划与回复）为基础。实施与验收见[3.65 任务单](tasks/3.65-chat-parity.md)，不包含 3.64 前端工作。

## 对照范围

- Aureus 取本地 `30d4678`。从 QQ 入站、收件箱、规划器（决定做什么）、回复器（决定怎么说）一直查到发送回执及下一轮实际读到的历史。人工平台操作不等于小豆能自主完成。
- MaiBot 取指定安装目录内的软件：发行元数据确认核心 **1.3.15**，插件目录内共有 **73 份** `_manifest.json`，包含 SnowLuma 适配器 **1.1.8**。对关键动作进一步核对了核心与插件源码，而不是拿 GitHub 最新版或插件名字充当行为证据。
- **已安装、程序能做、旧实例实际启用，是三件事。** 没有读取运行配置、数据库、日志、账号名单或真实聊天。按能力先排计划；还原旧开关时只需维护者提供脱敏的「插件 ID、启用状态、希望保留的效果」，不需要整份配置。不同插件实现同一效果时只移植一次。
- 不重建已有的群私路由、数组形式的 @ 和昵称唤醒、显式发言/沉默、防抖与规划打断、工作记忆、人物/表达/黑话召回、当轮看图与语音转写、分段、已登记表情选择和图文前后置。应补的是这些能力接不到的实际场景。

## 22 项可以单独验收的行为

### 第一批：先让小豆答对人、接得上话

| # | 能力 | 现状 → 要验收的改变 |
|---|---|---|
| 01 | 多人聊天选对回复对象 | 目前一批消息默认以末条选人、取画像、回投。甲问集合时间，乙随后聊午饭，小豆能答甲；**答谁**和**要不要显示 QQ 引用**分别决定。只能选本会话真实候选，不能编平台号。 |
| 02 | 收到点名时也听见前情 | 优先 @ 路径可把旁边普通消息标已读却没带进这次回复。甲说「七点南门」，乙 @ 小豆问「你来吗」，小豆能用到时间地点。邻句只作背景，不重开旧积压；分清 @我、@别人、@全体。引用小豆问话而没 @ 也能叫到它，引用别人不因此唤醒；叫到不等于抢占别群。 |
| 03 | 读懂别人引用的旧消息 | 当前提示主要是引用编号。引用昨天的话问「这个还算吗」，应取回同会话可读的原话和作者；目标缺失或已撤回不补写原文。不要把引用了谁误当成是谁在发问。 |
| 04 | 分清群聊、私聊、熟人和陌生人 | 当前非主人可能一律收到「你还不认识对方」提示，与已知画像冲突。认识的好友私聊不被当成陌生人，群里甲问乙时不替乙抢答；甲问「还记得我喜欢什么吗」，不会因乙插话取出乙的喜好。认识不等于有主人的权限。 |
| 05 | 记得自己**实际**说过哪句 | 目前下一轮可能看到完整待发草稿，虽已有逐段回执。三段只发成一段时，区分已说、待发和失败；撤回后也不再拿那句话当可见历史，又不会重复补一遍已发段。 |
| 06 | 隔一轮还能接着聊刚才的图片或语音 | 已经会当轮看图/转写，但普通历史走的是原消息正文。先发图再问「左边那个呢」，或先发语音再问「刚才说的几点」，能找到对应附件已有的说明或转写；未识别就说未识别。 |

### 第二批：补齐 QQ 里的实际动作

| # | 能力 | 现状 → 要验收的改变 |
|---|---|---|
| 07 | 正文后自动补发表情包 | 已有主动选图和图文排序，但必须先有选图建议，正文就绪后默认再等 0.8 秒，超时丢弃。补两种情形：当轮没选图，文字发完后按策略决定是否补；选图稍慢，文字先走，有效轮内可以补。**同一轮至多补一张**，已发图、不合适、换话题或被打断时不补。 |
| 08 | 不凑文字，只回一张表情包 | 目前正文为空就结束。允许明确选择「用图回应」：一张合适的图即可，不伪造空文本气泡；没有合适素材可以沉默。 |
| 09 | 群友斗图、复读时自然接一次 | 区分同一情绪的斗图与原样复读：前者从已批准的库选合适图，后者可跟文字/已允许的图或原生表情；不跟自己的消息，群聊刷屏不形成循环。 |
| 10 | 看懂并发送 QQ 原生小黄脸 | 原生 `face` 不等于图片表情包：当前纯小黄脸可能整条丢失，夹文字也丢表情。保留顺序和含义；发一个原生表情在 QQ 上确实是表情，不是「[捂脸]」几个字。`emoji2face` 在这份安装中只是配置入口，真实编码在 SnowLuma 适配器。 |
| 11 | 给特定消息贴表情、看见别人贴的回应 | 把回应挂在目标消息下，不发一条新气泡；正确识别哪条消息被谁回应。与发表情包、发原生小黄脸分开验证。 |
| 12 | 小豆主动戳熟人 | 现有人工接口能戳，聊天规划不会选。对齐智能戳插件的群聊主动模式：能选当前群实际成员，按冷却控制频率，不把群聊策略悄悄搬到普通或临时私聊。 |
| 13 | 被戳后作出反应 | 当前通知只归档。别人戳小豆，可以回戳、回一句、回图或不理；群友互戳不误认成戳自己，不互相无限回戳。 |
| 14 | 撤回自己发错的指定消息 | 用成功回执定位**本账号在本会话**发出的那一条；可按选中消息撤回，也可引用自己的旧消息请求撤回。平台拒绝或超时不声称撤回成功；这不是删本地记录或群管撤别人发言。 |
| 15 | 尊重对方撤回，必要时跟撤 | 收到有效通知后，目标先撤就不再答、引或复述；已经回复，可单独开启跟撤，只撤与目标直接对应的己方消息。旧跟撤插件明示只适配 NapCat，已回复的私聊还可能收不到通知；SnowLuma 需单独验群/私聊，不承诺无通知也能跟撤，不另开旁路连接。 |
| 16 | 群里真正 @ 指定的人 | 人工发送能 @，自动回复主要发文字、图和引用。支持直说、引用、@、引用加 @；只选真实群成员，后续分段不重复 @，普通私聊不制造 @。 |

### 第三批：续话、看转发和媒体、说话自然些

| # | 能力 | 现状 → 要验收的改变 |
|---|---|---|
| 17 | 「你等我一下」：等一会再看 | 现有防抖和 `stop` 都不等于 MaiBot 的定时回看。对方说还要发图时，不抢着总结；新消息到来可结束等待，到时可决定继续或沉默。只回看原会话。 |
| 18 | 说完后按需要续一句 | 分段是一份草稿拆几个气泡，续话是已经说完后判断还有没有**新内容**。复用 `cookies_self-talk` 和私聊拟人化的效果；有人接话则让出，没新内容就停，不能自行循环。 |
| 19 | 回找并发回指定旧图 | 能检索图片不等于能将那张原图发回。参照「旧账回溯」和核心 `attach_pic`：同会话问「再发一下刚才第二张」能按消息与图片位置找到，能看、能发分别核权限；已移除或不可用就不换别的图冒充。 |
| 20 | 看懂合并转发和分享卡片 | 现在纯合并转发可能整条丢。能回答「转发里谁说改期」，不把转发里的话当成现用户的指令。分享卡片只有标题就只据标题作答；有授权且取得视频字幕或章节时再回答「第二段讲什么」，不自动多发一条摘要。不是向别群转发。 |
| 21 | 聊普通动图、视频和上传的文件 | 已有 GIF **表情**多帧处理，不是所有动图都走时序理解。分三个子验收：普通动图能说清前后动作；视频/音频按问句取相关片段；对聊天附件里的文件按内容答问。维护者能上传文件不等于小豆能读文件。 |
| 22 | 正文净化、分段和纠错 | 已有口语指令、回复再审、强制分段与保护标记。补「只有明确标记才分气泡，没标记整条发」；命令、链接和代码不被误切。过滤泄进正文的角色/思考包装；可自然更正或撤回重发。不把工具报错原样发群、不谎报成功；刻意打错字另选。 |

## 6 项可单选的插件能力

| # | 能力 | 怎么验收 |
|---|---|---|
| 23 | 不实施：指定账号只作背景 | 维护者排除本项，不引入账号名单、背景模式或机器人屏蔽开关。 |
| 24 | 知道自己加入了哪些群 | 在允许的会话能据实际群名单回答「你也在某群吗」；不是列出群成员，更不是向陌生人公开全部群号。 |
| 25 | 查通用网络梗 | 区别于从群里学到的黑话；被问时能查到释义、用法或出处，查不到就不编。 |
| 26 | 定时提醒 | 能创建、查询、取消提醒。旧插件到点后向当前或指定会话提请小豆决定是否开口，不保证送达；「明确约定时必须送达」是另一个可选要求。 |
| 27 | 主人的跨会话短期叮嘱 | 主人交代「下次在那个群聊到这事时提醒一下」，影响目标会话下一次判断；开口后消费，或超时失效。不同于定时提醒，也不是替主人代发原话。 |
| 28 | 受托发文件或普通图片 | 目前能人工上传；让小豆按维护者授权把指定文件发回当前群/私聊，普通图片按图片气泡发送。与 21「读收到的文件」独立，不开放任意本地路径。 |

## 源码锚点与现成回放

以下行号针对上述核对版本。插件路径写成「目录名 + 文件」，相对安装的 `Profile/modules/MaiBot/plugins/`；核心路径相对安装包的 `site-packages/`。通用源码不需要旧实例运行。

| 要点 | Aureus | 对照 |
|---|---|---|
| 选谁、引用、点名 | `aureus/heartbeat/conversation.py:333`、`aureus/heartbeat/planner_tools.py:292`、`aureus/heartbeat/main_loop.py:1915`、`aureus/heartbeat/context.py:472` | 核心 `src/maisaka/builtin_tool/reply.py:124` 选消息，`:212` 可真 @，`:300` 独立决定引用；适配器 `snowluma-adapter/codecs/inbound/message_codec.py:432` 解析被引原件。 |
| 当前群/私和认人 | `aureus/heartbeat/context.py:81`、`aureus/heartbeat/people_context.py:31`、`aureus/heartbeat/conversation.py:1150` | `cooki_at-marker`、`cooki_identity-guard`、`deepseek-v4-pro_private-humanizer` 分别处理 @ 指向、熟人名字和私聊语境。 |
| 投递与后续记忆 | `aureus/heartbeat/context.py:480`、`aureus/heartbeat/reply_records.py:29`、`aureus/channels/qq.py:431` | `tests/heartbeat/test_reply_splitting.py:239` 体现第一段发出时，下一轮已看到全部草稿。需增加后段失败、撤回的回放。 |
| 自动补图 vs. 已有配图 | `aureus/heartbeat/conversation.py:842`、`aureus/heartbeat/conversation.py:886`、`tests/heartbeat/test_emoji_selection.py:402`、`tests/channels/test_qq_emoji_replay.py:47` | **更好的消息后处理 1.1.3**：`github_cateye_better-post-processing/plugin.py:3201` 检查发言轮是否结束及是否已有图，`:3310` 观察斗图。**Emoji Text Selector 2.1.0** 负责选图；核心 `src/maisaka/builtin_tool/send_emoji.py:56` 支持独立发表情。 |
| 戳、撤回、贴表情 | `aureus/channels/qq.py:143`、`aureus/channels/qq_desktop.py:101`、`aureus/channels/onebot.py:156` | **智能戳 1.11.1** `TAIY2020_smart_poke_plugin/plugin.py:656`；**麦麦撤回 0.1.3** `github_cateye_maimai-recall/plugin.py:454`；**撤回跟随 0.5.0** `github_ZhaoCang-QWQ_recall-follow/plugin.py:432`；**贴表情 2.1.1** `davidblackcn_maibot-message-react-plugin/plugin.py:320`。适配器 `snowluma-adapter/apis/message.py:64` / `:101` 有对应撤回/贴表情接口。 |
| 等待与续话 | `aureus/heartbeat/planner_tools.py:307`、`aureus/heartbeat/planner_tools.py:424`、`aureus/heartbeat/reply_segments.py:103` | 核心 `src/maisaka/builtin_tool/wait.py:46`；`cookies_self-talk` 以 `reply`、`stop`、新入站区分一轮；不能用发三个气泡代替验收。 |
| 多媒体与转发 | `aureus/channels/onebot.py:173`、`aureus/heartbeat/inbox_media.py:116`、`aureus/heartbeat/platform_history.py:121`、`aureus/heartbeat/emoji_recognition.py:61` | 核心 `src/maisaka/builtin_tool/reply.py:178`、`query_image_memory.py:55`、`view_forward_message.py:86`；`github_fabiocsa_read-gif/plugin.py:77`、`github_saltinanon_video-vision/plugin.py:222`、`org_mai-mai_file-reader/plugin.py:746`。 |

## 73 份插件清单怎么处理

每个安装目录只列一次；**按用户能感受到的效果移植，不把旧宿主、另一条 QQ 连接或多套后处理一起搬进来**。下表是全量归属，不代表每个插件已逐行审完或需要默认开启。01–22 是聊天候选，23–28 是可单选的插件能力；非基础聊天也保留去向。

| 能力组 | 安装目录（每个仅列一次） | 去向 |
|---|---|---|
| 认人、群聊注意力（6） | `cooki_at-marker`、`cooki_attention-focus`、`cooki_identity-guard`、`cooki_group-awareness`、`github_hekat87_mute-user`、`github_cateye_machine-crisis` | 先做 01–04；复用会话驻留和人物档案。另验“我在哪些群”“某机器人发言只作背景不回”；不把同类识别和用户封禁混为一个开关。 |
| 历史、规划、再审（8） | `cooki_continuity`、`cooki_chat-history`、`cooki_recall-archive`、`cooki_reference-fold`、`com_0-hz_maibot-corpus-callosum`、`github_cateye_filter-assistant`、`cooki_planner-slim`、`cooki_reply-sanitizer` | 接入 01–06、18、19、22。已有再审、历史查询、持久工作记忆和重启续接先测等效；补精确旧话/旧图、撤回同步。续话可选同一事件已回应就不再追问；不照搬跨会话原文直读。 |
| 语气、分段、私聊续话（8） | `cookies_self-talk`、`deepseek-v4-pro_private-humanizer`、`cooki_split-control`、`cookies_post-processing`、`cooki_style-guard`、`WiseXin_character-disguise`、`cooki_send-failure-gloss`、`cooki_breakdown` | 04、17、18、22。只用一个发送队列；风格不冒充身份，失败可以说得自然但必须属实。特殊连喊等独立可选，不拿来提高普通聊天评分。 |
| QQ 表情与互动（8） | `github_cateye_better-post-processing`、`hsd221_emoji-text-selector`、`juzi865_emoji2face`、`davidblackcn_maibot-message-react-plugin`、`TAIY2020_smart_poke_plugin`、`maibot-community_mai-repeater`、`github_cateye_maimai-recall`、`github_ZhaoCang-QWQ_recall-follow` | 07–16、22。已有选图器保留；贴表情、发表情包、原生表情、主动戳、回戳和撤回分别核对真实平台动作。 |
| 人际、黑话、提醒、身份（7） | `github_ZhaoCang-QWQ_social-state`、`github_beichen24a1_memewiki`、`github_jiakeju_maibot-curator`、`github_42yoru_maimai-owner-reminder`、`xhstoyea_maibot-reminder`、`valleywinds_self-identity`、`cooki_persona-presets` | 04 复用人际/表达/日程；25–27 分别验通用梗百科、定时提醒意图和主人的短期跨会话叮嘱。curator 是后台整理黑话、表达、表情和群友日程，不是提醒器，不直接搬改库逻辑；身份检索和切换预设另列，不自动改人设。 |
| 聊天附件和分享内容（7） | `github_fabiocsa_read-gif`、`github_saltinanon_video-vision`、`org_mai-mai_file-reader`、`tom_listen-audio-file`、`YukiSakiko_content_understanding_plugin`、`omsociety_bilicaption`、`cooki_file-upload` | 06、19–21。分别验收到的附件、分享卡片、视频链接和受控发送；能读语音条不等于能听上传的音频文件，能看卡片不等于已看过整段视频。 |
| 点单与娱乐（7） | `cooki_weather`、`xxxxx7258_google-search-plugin`、`github_pan-ice_music`、`github_FleetingEternity-qyt_qq-like`、`deepseek-v4-pro_random-image-api-for-maibot`、`sengokucola_maimai-niuniu-plugin`、`com_octmicy_vw50` | 基础轮后做请求式天气、查资料/缩写、点歌、名片赞和可选娱乐；名片赞不是消息表态。外部素材、商业/收款类能力保留独立开关。 |
| 平台、跨群与治理（6） | `papaya0481_forward-messages-auto`、`qq_official_adapter`、`snowluma-adapter`、`deepseek-v4-pro_maimai-group-admin`、`octmicy_mai-gatekeeper`、`whitefox_friend-request-handler` | 基础 QQ 以已部署的 SnowLuma 路径验收。跨群转发、官方平台适配、群管和好友申请另列授权任务，不给普通聊天动作管理员权限。 |
| 生活、空间与内容来源（12） | `antinova_maibot-bililife`、`cooki_bili-language-bridge`、`cooki_bili-schedule`、`cooki_bililife-dialogue`、`cooki_maitrace-space-images`、`github_erhan111119_mai-repost-shit`、`internetsb_maizone`、`Rabbit-Jia-Er_MaiTrace`、`maibot-community_mai-life`、`DeepSeek-V4-pro_life-behavior-reference`、`cooki_phone-bridge`、`hohoko-chan_phone-play` | 后续生活/外部行动批次。能把有来源的经历带进聊天，和主动对外浏览、评论、发动态或搬运内容，是不同权限，不算基本收发已完成。 |
| 工作工具、设备与观察（4） | `chartyr_maiwork`、`cooki_dsh-tool`、`cooki_sandbox-startup`、`cooki_thought-stream` | 后续工具批次：群任务协作、获准的外部工作、个人环境集成、可读的行动摘要分别评估；不复制旧执行环境、私人设置或内部推理日志。 |

## 实施顺序与“智商”验收

1. **建一套两边共用的假群聊/私聊回放。** 以同一批合成事件、假时钟、假模型和模拟 QQ 端记录「该不该答、答给谁、看到了哪些原话、实际发出什么」。原有行为测试是起点，不把模型说“做了”当作 QQ 做了，也不要求两边逐字一样。
2. **第一批先交付，再开放社交动作。** 同一发言者/消息/会话和逐段发送回执贯穿 01–16。先通过甲问乙插话、旧话被引、熟人私聊、图/语音追问和后段失败这几组，再加补图、戳、撤回等；不能靠提高发言率掩盖答错人。
3. **第二批和第三批按动作分别验收。** 重点回放：选图晚到 / 本轮已发图 / 换话题；群友互戳 / 戳小豆 / 连戳；撤回在发送前 / 发送后 / 平台拒绝；先说等图再来图 / 一直没来；嵌套转发里的「我」属于谁。每组都同时断言不该发生的动作 **0 次**。
4. **插件归属收口。** 按表逐项登记旧开关是否已确认、Aureus 是已有等效/待补/另行授权、许可证和可复现用例。重复的分段与表情插件共用一个出口；源许可未核实只借鉴行为。再用同一组编造对话盲比“接题、连贯、不抢话、动作自然”，另记首段是否被慢动作拖住。真实模型或测试群验收另行确认，本轮未进行。

**已确认的边界：** D103 要求显式动作，独立表情、续话、戳不能当作失败兜底。D117 的旧规则是超时就放弃，07 改为“文字不等、有效轮内可晚补一次”。D145/D146 仍不追积压，02 只补邻近背景。平台原文与 D141 的跨会话长期记忆分开；不能借查询原图、原话或执行动作把原聊天发到别处。保留表情审批、平台访问范围、急停和预算。完整插件清单是对照资料，不扩展此次批准的 27 项。
