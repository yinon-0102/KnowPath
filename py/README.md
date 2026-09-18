# Keel

> 一个不会偏航的 AI 伙伴。

<p align="center">
  <img src="https://img.shields.io/badge/python-≥3.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/uv-managed-purple" alt="package manager">
</p>

Keel 是**一个 agent**——一个有长期记忆、会自证、懂分寸的 AI 伙伴。

名字取自船的「龙骨」:那根贯穿船体、撑住稳定性与航向的结构。大多数 agent 要么聊完就忘、要么任务没做完就嘴上说"完成了"、要么给把 `rm` 就敢闯祸。Keel 想成为相反的那一个——模型负责聪明,**她负责靠谱**:记得住、做得完、不闯祸。

---

## Table of Contents

- [🧠 她是真的"记得"](#她是真的记得不是把历史硬塞回去)
- [✅ 她会"自证完成"](#她会自证完成而不是嘴上说说)
- [🛡️ 她"先问再动手"](#她先问再动手不会替你闯祸)
- [🎛️ 上下文编排器](#她有个真正懂上下文工程的大脑)
- [🎨 五种思考方式](#她还会换着法子干活)
- [用起来什么效果](#用起来什么效果)
- [实际跑一段给你看](#实际跑一段给你看)
- [Quick Start](#quick-start)
- [Configuration Reference](#configuration-reference)
- [What she's made of](#what-shes-made-of)
- [Her philosophy](#her-philosophy-design-principles)
- [Project Status](#project-status)
- [Contributing](#contributing)
- [License](#license)

## 🧠 她是真的"记得",不是把历史硬塞回去

很多 agent 靠把对话历史一股脑灌进 context 来"假装有记忆",窗口一满就忘、就乱。Keel 给的是一套**真正的分层记忆**:

- **L0 偏好卡片 / L1 工作窗口 / L2 滚动摘要 / 冷存储**,层层有职责,可配置可插拔;
- 一张**时态知识图谱**——内核是"时态 + 冲突消解":同一件事你前后说法变了,她知道哪条**现在**有效,而不是把矛盾的事实一起记着;
- **用户层 + 项目层双层记忆**,好经验还能**跨项目提升**——在 A 项目摸清你的偏好,B 项目直接受用;
- 顶上压着一个**诚实契约**:她敢凭印象瞎答历史信息,就会被拦下来,触发真正的召回,而不是编一个看起来对的答案。

她把"记性"当成一等公民来设计,而不是挂了个 memory 模块。

## ✅ 她会"自证完成",而不是嘴上说说

agent 最大的谎言是"我做完了"。Keel 不吃这一套:

- **验证闭环**:定目标 → 执行 → 检查 → 算残差 → 判收敛,没达标就自动重规划重试。"完成"是**检出来的**;
- **真 TDD 模式**:独立出题器先写测试,经**硬红门**确认"实现前测试确实是红的",再用**哈希防篡改**把测试锁死——从机制上杜绝"先写实现再补测试""边改边标完成";
- **离线评测**:用例驱动地回归她的行为,是开发时手里那只稳稳的"方向盘"。

她把"完成"从一句空话,变成一道道过不去就不放行的门。

## 🛡️ 她"先问再动手",不会替你闯祸

- 危险操作(比如跑一条 `bash` 命令)触发**三态审批**:完整改动落进 scrollback 任你上滑核对,紧凑框常驻"允许 / 仅本次 / 拒绝";
- 文件操作从不直接落盘,先进 **Workspace 沙箱**暂存,审阅通过才导出,还带 deny set 防误删。

## 🎛️ 她有个真正懂"上下文工程"的大脑

记忆只是"源",真正决定每轮往模型嘴里喂什么的,是她的 **上下文编排器**:按**保底 + 共享池预算**精打细算地组装上下文,预算不够时**只丢弃、绝不截断**——保证送进去的每一块都是完整的,而不是被腰斩的半句话。

## 🎨 她还会换着法子干活

需要不同打法,她有**五种思考方式**——直接问答、原生工具调用、ReAct 思考-行动-观察、先规划再执行、输出后自我反思。配上一身 Claude Code 风的 CLI:Slash 命令、Markdown 渲染、主题、状态栏、钉底 todo 面板,体感一个不少。

---

## 用起来什么效果

- **你随口提过的偏好,她下次还记得**,而且不会被你后来改过的说法搞糊涂——不用每次都重新交代一遍;
- **她说"做完了",就是真的过了验证**,不是糊弄你,省下你逐条复查的功夫;
- **她碰你机器上有风险的东西之前,一定先停下来问**,不会自作主张删文件、跑命令;
- **跨项目用得越久,她越懂你**——这个项目学到的习惯,下个项目直接带过去;
- **上下文从不被腰斩**,长对话里她也不会丢掉关键前提、答出前言不搭后语的话。

一句话:**模型负责聪明,Keel 负责靠谱。** 她不是那种"演示惊艳、上手翻车"的 agent,而是越用越让你放心交东西给她的那种。

---

## 实际跑一段给你看

下面是一段**真实对话**(逐字未改,仅去掉重复日志行)。注意她怎么**主动把记忆当工具来管理**——存的时候自己调 `remember`,用的时候自己调 `recall`,而不是把历史一股脑塞回 context:

```
你 ▸ 我习惯用 4 空格缩进,别用 tab。

Keel ▸ 🔧 remember ← 用户偏好缩进风格为 4 空格,不要使用 tab
       好的,已经记录:你习惯使用 4 空格缩进,不要用 tab。
       之后的代码或格式化内容,我会一律使用 4 空格。

   …(中间隔了一整轮无关的问题:Python 列表推导式怎么写,她照常作答)…

你 ▸ 我刚说我缩进习惯是什么来着?

Keel ▸ 🔧 recall ← 用户关于缩进习惯的信息
       根据你刚才说的——你习惯用 4 空格缩进,不要用 tab。
       我已经记住了,随后的代码中也会采用 4 空格风格。

你 ▸ 帮我算 15 * 8 + 32

Keel ▸ 🔧 calculator ← 15 * 8 + 32
       15 × 8 + 32 = 152。
```

隔了一轮无关对话,她依然准确取回了你的偏好,还明说"根据你刚才说的"——**Recall real memory, don't make up a plausible answer from vague impressions.** That's the honesty contract + layered memory at work.

---

## Quick Start

她用 [uv](https://github.com/astral-sh/uv) 管理一切,**不用你手动装 Python**——`uv sync` 会自动拉好 Python ≥ 3.13 和全部依赖。下面按系统选一份照抄即可。

<details open>
<summary><b>🍎 macOS</b></summary>

```bash
# 1. 装 uv（已装可跳过）
brew install uv                                   # 或: curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 拉代码
git clone https://github.com/HHHH-LK/keel.git && cd keel

# 3. 装依赖 + 配置
uv sync
cp .env.example .env                              # 然后编辑 .env 填 key/model/base_url

# 4. 启动
uv run keel
```
</details>

<details>
<summary><b>🐧 Ubuntu / Linux</b></summary>

```bash
# 1. 装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env                       # 让当前终端认到 uv(或重开终端)

# 2. 拉代码
git clone https://github.com/HHHH-LK/keel.git && cd keel

# 3. 装依赖 + 配置
uv sync
cp .env.example .env                              # 然后编辑 .env 填 key/model/base_url

# 4. 启动
uv run keel
```
</details>

<details>
<summary><b>🪟 Windows (PowerShell)</b></summary>

```powershell
# 1. 装 uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. 拉代码
git clone https://github.com/HHHH-LK/keel.git
cd keel

# 3. 装依赖 + 配置
uv sync
Copy-Item .env.example .env                       # 然后编辑 .env 填 key/model/base_url

# 4. 启动
uv run keel
```
</details>

> No API key? You can still launch and set it inside with `/config key`. The `.env` expects `LLM_API_KEY` / `LLM_MODEL_ID` / `LLM_BASE_URL`. Model providers are swappable: OpenAI / Anthropic / Gemini / local models.

Once inside, try Slash commands: `/help`, `/config`, `/memory`, `/clear`. The legacy entry `uv run python chat.py` is also kept.

---

## Configuration Reference

| Key | Required | Description |
|-----|----------|-------------|
| `LLM_API_KEY` | ✅ | API key for your preferred LLM provider (OpenAI / Anthropic / Gemini / local) |
| `LLM_MODEL_ID` | ✅ | Model name, e.g. `gpt-4o`, `claude-sonnet-4-20250514`, `gemini-2.5-flash` |
| `LLM_BASE_URL` | ❌ | API base URL; defaults to OpenAI. Set to a local endpoint (e.g. `http://localhost:8000/v1`) for self-hosted models. |

All other settings can be configured interactively via `/config` once inside.

---

## What she's made of

```
keel/                     # Python 包(她叫 Keel)
├── core/        # Agent 基类、LLM 客户端、Message、Hook、Config
├── agents/      # 五种思考方式
├── context/     # 上下文编排器
├── memory/      # 分层记忆 + 时态 KG + 双层记忆
├── tools/       # 工具注册与执行链
├── verify/      # 验证-重试闭环
├── tdd/         # 真 TDD 模式
├── planning/    # 任务 todo 规划
├── bench/       # 离线评测
├── cli/         # Claude Code 风 CLI
├── workspace/   # 文件操作沙箱
└── test/        # 单元 + 集成测试
```

设计文档在 `docs/superpowers/specs/` 与 `docs/superpowers/plans/`。

---

## Her philosophy (design principles)

- **轻量优先**:核心结构清晰,不在早期堆重依赖
- **可控优先**:每项能力都有明确边界,而不是把权力全丢给模型
- **可验证优先**:完成靠检验,不靠自报
- **面向运行时**:在意"持续把任务做完",而不只是"生成文本"

---

## Project Status

Keel is currently in **active development (alpha)**. The core architecture is stable, but APIs may evolve as we add features and polish. We'll tag a stable release once the design settles.

What to expect now:
- ✅ Core agent loop, layered memory, context orchestrator, tool execution
- ✅ True TDD & verification-closed loop
- ✅ CLI with themes, status bar, Markdown rendering
- 🚧 More tool integrations (web browsing, file search, etc.)
- 🚧 Plugin system for custom tools & memory backends
- 📝 Comprehensive multi-language docs

---

## Contributing

Contributions are welcome! Here's how:

1. **Open an [Issue](https://github.com/HHHH-LK/keel/issues)** — bug reports, feature requests, design discussions
2. **Submit a PR** — fork → branch → commit → pull request
3. **Keep it clean** — the project uses `uv` for dev; run `uv sync` and make sure tests pass before opening

```bash
# dev setup
git clone https://github.com/your-fork/keel.git && cd keel
uv sync                      # install all deps
cp .env.example .env         # configure
uv run keel                  # try it
```

See the design docs under `docs/superpowers/specs/` and `docs/superpowers/plans/` for architectural context. No strict code style guide yet — just use good judgment and consistent formatting.

## Learning backend

后端工作目录是 `py/`，虚拟环境是 `py/.venv/`。在本目录执行：

```powershell
uv sync
uv run uvicorn my_agent_llms.learning.main:app --host 127.0.0.1 --port 8000
```

默认使用内存仓储。要保存资料、Run 状态与事件、学习空间元数据和画像，先启动项目 MySQL，再在 `py/` 执行：

```powershell
$env:DATABASE_URL = "mysql+pymysql://keel:keel@127.0.0.1:3306/keel_learning"
uv run alembic upgrade head
$env:LEARNING_PERSISTENCE = "sql"
uv run uvicorn my_agent_llms.learning.main:app --host 127.0.0.1 --port 8000 --workers 1
```

SQL 模式要求先完成迁移；启动时不再用 `create_all()` 隐式建表。`.env.example` 只是配置示例，以上命令显式设置环境变量。Docker 配置见 `../infra/docker-compose.yml`。

Run/SSE 当前支持递增事件 ID、UTC 时间戳、Last-Event-ID 续传、15 秒心跳、终态关闭和七天历史过期检查。取消请求先进入 `cancelling`，执行器确认停止后才能变成 `cancelled`；此期间拒绝发布迟到的结果和消息。SQL 启动时将残留的非 graph_reconcile / knowledge_publish 类 queued/running/cancelling 任务标记为 `failed / RUN_INTERRUPTED`；graph_reconcile 和已确认 knowledge_publish 由独立持久 worker 按租约恢复和重试。

当前 SQL 模式只支持单个服务进程；多个进程同时启动会错误地将彼此任务判断为中断。学习空间的基础字段、固定资料版本绑定、学习范围、画像及 space/scope/profile 版本号已持久化到 MySQL。测验冻结快照、答题修订、证据、掌握度、状态重置和 state_version 已随 0004 迁移持久化；重启后可恢复测验、评分结果、Run 和证据。计划、任务、学习会话、会话事件、评分复核审计和导出快照也已持久化。0009 迁移增加独立对话与消息表；出题和对话在事务提交后通过 FastAPI 后台任务调用 DashScope，模型不可用会将对应 Run 标记为 failed。0011/0012 已实现图谱持久 worker、真实图谱发布和知识纠错；空间版本采纳、受影响状态失效和局部重排已实现；其他异步任务的持久重试仍待实现。

资料上传和新增版本把资料、版本、片段、幂等记录、Run 与事件写入同一个 SQL 事务；任一步失败会一起回滚。相同 key 和请求在重启后返回原 material/version/run，不同内容复用 key 返回 409。旧幂等记录尚未关联 Run 时，会在首次匹配重试中补建关联，并发重试仍复用同一 Run。MySQL 默认 REPEATABLE READ 隔离级别下，上传按文件哈希加锁去重；锁冲突导致的死锁会有限重试。

创建空间与设置范围必须提供 Idempotency-Key；修改空间基础字段、画像的 PATCH 可选提供该键，提供后也保存并重放原响应。资源变化与响应快照在同一事务中提交。PATCH 和范围修改必须携带 expected_version；画像检查 profile_version，基础字段和范围检查 space_version。范围更新同时递增 scope_version，并将旧范围计划标记为 needs_replan。绑定版本在上传新版本后保持不变；范围只接受绑定版本中的实际主题。当前主题来自文本标题/片段，已发布图谱具有 Neo4j 来源投影；语义前置关系尚未提取，因此不自动扩展前置主题。

删除资料会在事务中检查持久空间引用；有引用时返回 409，即使请求 cascade=true 也不能绕过。完整级联删除、删除接口的确认/版本约束及业务清理仍待实现。幂等重放还覆盖测验创建、追加答案和状态重置；交卷依据测验终态复用原 Run。计划、学习会话事件、评分复核、导出和对话也支持幂等重放；图谱候选 reconcile 也已支持事务性幂等重放；图谱发布也已实现事务性幂等重放；24 小时幂等记录清理仍待实现。事件轮询和追加目前读取整个 Run 历史，尚未进行长对话负载优化；过期载荷在读取时清除，保留事件序号和时间用于判断续传是否过期。

测验和来源约束对话默认使用 DashScope，需要在服务进程配置 `DASHSCOPE_API_KEY`（请勿提交密钥）；默认聊天模型为 `qwen-plus`，Embedding 保持 `text-embedding-v3`。可设置 `LEARNING_CHAT_BASE_URL` 指向 DashScope 兼容服务。测试注入固定题集或模拟 HTTP，不消费真实模型额度。模型输出通过题数、题型、难度配额、答案键及来源引用校验后才发布；这些结构校验不能保证所有题意和答案在语义上正确，仍需人工评估。

答案采用追加修订，交卷将最新答案、逐题证据、掌握度和 Run 放在同一事务内保存。未答或未可靠判定的短答为 `unverified`，不按答错计分；请求题目提示后保留教学反馈，但排除独立掌握证据。状态重置保留历史，重置前的测验与旧资料版本不写入当前分数。`MasteryPolicy` 可注入聚合窗口、阈值、间隔和复习周期；默认取最近 20 个题目族的首次有效证据，满足至少 3 族、2 次测验、24 小时间隔及应用题覆盖后才可判定掌握。默认 7 天后标记待复习，时间流逝不会降低分数。题目族归一化是文本启发式，尚不能识别所有语义改写。

评分复核会持久化审计、替换证据并重算相关状态；不会删除旧证据链。学习空间删除要求 confirm=true 与 expected_version，在事务中清除该空间历史、导出、对话和关联 Run，保留其他空间及共享资料；删除后旧命令重放返回 410。导出为有效期 24 小时的 ZIP，采用公开字段白名单。

对话仅使用空间绑定资料版本和当前范围，最多选择 8 个片段。默认 `LEARNING_RETRIEVAL_BACKEND=keyword` 使用关键词；设置为 `qdrant` 后使用 DashScope `text-embedding-v3`、1024 维和 Cosine 做向量召回。检索前不再提前裁掉候选片段，Qdrant 命中必须匹配资料版本、图版本、chunk_id 与正文哈希；正文仍从数据库来源快照读取。session_id 为空时建立独立对话，指定时验证会话归属与学习会话活动状态。同一会话只允许一条生成中的消息。模型返回 JSON 后先校验引用，再经 SSE 分块发送；这是经校验后的分块输出，不是上游 token 级透传。活动测验中的明确索答请求转为提示并记录 assisted，不写入成绩；规则仍有自然语言识别局限。生成期间取消、删除空间、改变范围或结束学习会话均不会发布迟到正文。进程内后台任务不具备独立队列的恢复能力；服务重启后中断任务标记失败，需要用新的幂等键重新请求。

向量索引目前通过显式命令准备。先在项目根目录启动 Qdrant：

```powershell
docker compose -f .\infra\docker-compose.yml up -d qdrant
```

在 `py/` 中为需要使用的每个绑定资料版本执行（将占位值替换为 API 返回的 material_version_id）：

```powershell
uv run python -m my_agent_llms.learning.vector_indexing --version-id "<material_version_id>"
```

命令从 SQL 读取指定版本，要求资料已解析为 ready，使用 `DASHSCOPE_API_KEY` 调用真实 Embedding 服务并写入 Qdrant，因此会产生模型调用。它不会切换空间绑定或发布图谱；当前适配现有的 graph_version=1 主题投影。片段使用与对话一致的前 6000 字，批次最多 10 个；正文和题目答案键不写入向量 payload。重复运行按确定性 ID 覆盖同一组点。Qdrant collection 名由 `QDRANT_COLLECTION` 前缀加模型、维度和索引格式的摘要构成，不覆盖旧模型集合；首版客户端只接受 DashScope text-embedding-v3 / 1024，其他配置会启动失败。

全部绑定版本建好索引后，在 `py/.env` 设置 `LEARNING_RETRIEVAL_BACKEND=qdrant` 并重启服务。索引不完整返回 `VECTOR_INDEX_NOT_READY`，不会降级成关键词；失败的 Run 保留原结果，修复索引后使用新的 Idempotency-Key 重试。客户端连接超时为 Qdrant 30 秒、Embedding 每批 60 秒，不自动重试或跟随模型重定向。新版本需要重新建索引，原来固定的旧版本仍可查询。

这个命令是自动摄入前的维护入口。GraphService 的候选快照和 graph.prepare outbox 事件已持久化；跨存储 worker、带审核的发布和任务重试已实现（见下文），物理删除清理仍待实现；索引与资料删除并发时可能留下不可通过当前来源白名单检索的孤儿向量，后续删除流程仍须负责物理清理，不能把该命令当作完整发布/删除工作流。

运行学习模块回归测试（PowerShell 先展开测试文件）：

```powershell
$learningTests = Get-ChildItem .\my_agent_llms\test\test_learning_*.py -File | ForEach-Object { $_.FullName }
uv run python -m pytest @learningTests -q
```

可选 MySQL 集成测试需要已迁移的本地测试库。先设置 `$env:LEARNING_TEST_MYSQL_URL = $env:DATABASE_URL`，再运行同一测试命令；这些测试验证 Run 并发写入和接口续传，以及上传的跨应用重放、旧记录补关联、同 key 和不同 key 的并发上传；还覆盖空间同键并发更新、不同键版本冲突、旧快照下绑定新资料版本、空间创建与资料删除互斥；还覆盖测验与证据的重启恢复、同时交卷、同键原子提交、失败回滚、取消发布锁和删除保护；结束时只清理测试自身创建的数据。未设置此变量时会跳过 MySQL 用例。

设置 `$env:LEARNING_TEST_QDRANT_URL = "http://127.0.0.1:6333"` 可在同一回归命令中验证真实 Qdrant。每个测试只创建和清理带唯一随机前缀的测试集合；Embedding 使用测试向量，不调用真实模型。

---

## License

This project is open-sourced under the MIT License — see [LICENSE](LICENSE).

## Feedback

Questions or ideas? Feel free to open an [Issue](https://github.com/HHHH-LK/keel/issues) or PR.

---

如果你想要的不是"问一句答一句"的工具,而是一个**记得住、做得完、不闯祸**的长期 AI 伙伴——那就是 Keel。


### 图谱候选审核和发布（0011 迁移）

执行 `uv run alembic upgrade head` 后，reconcile 接口要求 `version_id` 和 `expected_graph_version`，并与候选快照、queued Run、待处理事件及幂等响应一起提交。首次正式发布前基版本是 0；旧空间使用的临时 graph_version=1 投影不代表已经发布的图谱。graph-diff 返回真实节点/来源差异，支持指定 revision_id 和 include_unchanged。

0011 迁移已补齐 graph.prepare worker、Neo4j/Qdrant 准备读回校验及原子发布。SQL API 启动保留 graph_reconcile / knowledge_publish / material_ingest 任务，由独立 worker 按租约恢复和重试；其他异步任务仍沿用原启动恢复规则。publish 对未就绪快照返回 409 REVISION_NOT_READY，对未决冲突返回 409 GRAPH_CONFLICTS_PENDING；发布不改变已有空间，新空间默认绑定最新发布的精确 revision。keep_both 保留双方来源，并保守排除整个冲突主题的自动出题。

在 py/ 目录运行（先配置本地 .env 或环境变量中的数据库、Neo4j、Qdrant、DashScope 连接信息）：

```powershell
uv run alembic upgrade head
uv run python -m my_agent_llms.learning.graph_worker_cli
```

API 必须设置 LEARNING_PERSISTENCE=sql 才能与 worker 共用任务。可加 --once 处理至多一个可执行任务；job_claimed 只表示已领取，最终状态请查询对应 Run。真实 embedding 会使用 DashScope text-embedding-v3（1024 维）。当前标题/片段提取不推断语义关系，上传后的自动图谱编排和外部物理清理仍待实现。详情见 ../docs/implementation/2026-09-19-graph-worker-publication.md。

### 知识纠错与确认发布（0012）

两个 knowledge-corrections POST 均要求 Idempotency-Key。创建时校验空间绑定的正式快照、目标和来源，事务保存纠错事件、不可变候选及等待确认的 Run，返回 correction_id、status=pending、candidate_revision_id；尚不入队外部准备。source_ref 是当前快照的 chunk ID。旧空间必须先采用最新正式快照才能纠错；可通过 knowledge-updates 预览并显式采纳已发布更新，再继续纠错。

replace 的 proposed_value 为非空字段补丁：node 支持 name、description；relation 支持 from_id、to_id、type（见 API 关系类型）。拒绝修改目标 ID、来源、版本等审计字段。reject 保留历史并标记目标 rejected；关系端点必须同属快照，prerequisite_of 不允许成环。

confirm 要求 expected_graph_version 和非空 reason，保存确认审计并入队 graph.prepare，返回 queued Run。独立 worker 准备 Neo4j/Qdrant 后，在同一业务事务发布、记录纠错结果并完成 Run；结果包含真实 graph_version、affected_topic_ids、update_available。直接 graph publish 不接受纠错候选，返回 CORRECTION_CONFIRMATION_REQUIRED。若其他发布抢先推进版本，本任务以 VERSION_CONFLICT 失败，需刷新后重新纠错。

同一资料版本再次 reconcile 保留已审核的有效快照，避免撤销已确认纠错；跨版本的节点审核字段变化需要显式解决冲突。

确认和发布不会移动已有空间绑定。空间删除会取消尚未完成的纠错任务，删除空间所属纠错记录，并保留资料快照审计；取消和外部准备失败不会发布结果。旧纠错数据迁移时保留，缺少候选上下文的旧记录需要重新提交。详情见 ../docs/implementation/2026-09-18-knowledge-corrections.md。

### 学习空间知识更新

GET /api/v1/learning-spaces/{space_id}/knowledge-updates 预览最新正式发布快照、受影响主题、历史题目及待重排计划，保持原空间绑定和分数不变。POST 同路径 /apply 要求 Idempotency-Key、expected_space_version 以及空间全部资料的 bindings（每项 material_id、material_version_id、graph_version）；拒绝缺失/重复资料、未发布候选、版本回退和陈旧空间版本。未更换快照返回 409 NO_KNOWLEDGE_UPDATES。响应中的 graph_revision_id 与 topic_revision_ids 是服务端解析数据，提交 bindings 时只传上述三个字段。

采纳在同一事务切换精确图谱绑定、递增 space/scope 版本、将受影响掌握度持久化为 stale / needs_review，并将旧计划持久化为 needs_replan、递增计划版本；Run 完成结果保存真实影响范围和 stale_state_count。未改变的主题跨资料版本沿用证据版本；变更或移除主题保留历史分数、题目和来源，但旧题与旧评分复核不能恢复新版本掌握度。显式学习范围保持选中的 ID，节点移除不会扩大为全部主题。无需新迁移，使用现有 JSON 绑定与计划配置列。

已有计划通过 plans 的 local_replan 流程重建，使用失效后最新 expected_plan_version；返回独立 plan_build Run。旧完成/跳过任务保留为 historical，新版本安排 diagnostic；include_review=false 也不会跳过 stale 掌握度的重新验证。采纳接口本身不自动生成计划，plan_replan_run_id 为 null。出题期间绑定发生变化时，保留生成题目为 stale 历史并以 STALE_INPUT 结束 Run。详情见 ../docs/implementation/2026-09-18-knowledge-updates.md。

### 原文件保存与自动入库（0013 迁移）

先在 py/ 执行 `uv run alembic upgrade head`，建立独立原文件表 material_raw_files。原文件、版本、幂等记录及解析任务在同一事务提交；单文件最多 20 MiB，文本 PDF 最多 300 页，文本使用 UTF-8。

POST /api/v1/materials 与 POST /api/v1/materials/{material_id}/versions 接受 auto_ingest，默认 true：上传返回 processing、0 个来源片段及真实 queued Run。auto_ingest=false 只保存原文件，返回 uploaded 和 run_id=null；新版本还接受 change_note，并将其纳入请求指纹。

POST /api/v1/materials/{material_id}/ingest 要求 Idempotency-Key，JSON 仅包含 version_id。未解析版本排队读取原文件；已有来源版本直接准备图谱。继续使用上面的 graph_worker_cli 独立进程，它消费 material.parse 和 graph.prepare；--once 最多认领一个任务，因此一次调用不保证两个阶段都完成。解析不持有写事务，解析结果与下一阶段任务原子提交，两个阶段共用一个 Run。准备成功后候选为 pending_review，仍需显式 publish。

相同幂等键重放原 Run；失败/取消后使用新键重试。候选 ID 在解析前为 null，完成后可在 Run.result_ref 查询。重启后 worker 可接管到期租约；过期、取消或删除中的任务不能补写来源。相同内容按解析格式去重，修正错误扩展名允许重新解析。已有归档状态、发布快照与学习空间绑定不会被后台解析覆盖。

旧资料若无原文件但有已解析来源，仍可准备图谱；若两者都缺失则返回 MATERIAL_SOURCE_MISSING，需重新上传。运行生产 worker 前需配置数据库、Neo4j、Qdrant 与 DashScope，默认 Embedding 为 text-embedding-v3（1024 维）。详见 ../docs/implementation/2026-09-18-automatic-material-ingest.md。


### 学习服务完整启动与本地安全配置

后端和虚拟环境均位于 `py/`。`.env` 使用 `LEARNING_PERSISTENCE=sql`，数据库、Neo4j、Qdrant 连接与现有本地环境一致；不要覆盖已有配置。先执行 `uv run alembic upgrade head`，再在三个终端分别启动：

```powershell
uv run uvicorn my_agent_llms.learning.main:app --host 127.0.0.1 --port 8000
uv run python -m my_agent_llms.learning.graph_worker_cli
uv run python -m my_agent_llms.learning.model_worker_cli
```

API 与 worker 自动加载后端 `.env`。生产入口使用 `LEARNING_LOCAL_TOKEN`；为空时生成并持久保存 `py/.learning-token.local`，前端读取配置后通过 `X-Local-Token` 传入，SSE 同样使用请求头。不要提交或在日志中打印令牌。`LEARNING_ALLOWED_ORIGINS` 配置允许访问的浏览器 Origin。未认证 health 只有基本状态；带令牌才能查询真实 MySQL/Neo4j/Qdrant 探测和模型配置状态。健康探测不执行付费模型请求。

`LEARNING_CONFIG_FILE` 可指向 JSON 配置，结构见接口文档 1.3；环境变量覆盖文件，文件覆盖默认值。默认聊天 qwen-plus、Embedding text-embedding-v3（1024 维）。默认 keyword 召回可用于本地功能验证；正式向量召回设置 `LEARNING_RETRIEVAL_BACKEND=qdrant`，索引由 graph worker 在发布前准备。

测验和消息任务通过 outbox 持久化，重启和限流重试保留原 run_id。默认最多认领 3 次，重试间隔 2、4 秒。异步错误通过 Run/SSE 查询，已返回的 202 不会变成另一个 HTTP 响应。模型能力不支持在入队前拒绝。model worker 的 `--once` 只尝试领取一个到期任务；要持续执行退避重试需保持常驻进程。

资料 DELETE 必须带 expected_version 和 confirm=true；被空间引用时还需 cascade=true。事务内清理学习数据并撤销相关模型任务，外部 Neo4j/Qdrant 清理由 graph worker 执行并读回验证。资料删除 Run 成功前，不能视为外部物理清理完成。空间删除保留其他空间共享资料。

接口验收及仍有的性能、部署限制见 `../docs/implementation/2026-09-19-api-completion.md`。学习接口代码未调用 Shell 或任意代码工具；现有 CLI agent 是另一个入口。
