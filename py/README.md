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

Run/SSE 当前支持递增事件 ID、UTC 时间戳、Last-Event-ID 续传、15 秒心跳、终态关闭和七天历史过期检查。取消请求先进入 `cancelling`，执行器确认停止后才能变成 `cancelled`；此期间拒绝发布迟到的结果和消息。SQL 启动时将残留的 queued/running/cancelling 任务标记为 `failed / RUN_INTERRUPTED`，不自动重放业务操作。

当前 SQL 模式只支持单个服务进程；多个进程同时启动会错误地将彼此任务判断为中断。学习空间的基础字段、固定资料版本绑定、学习范围、画像及 space/scope/profile 版本号已持久化到 MySQL。测验冻结快照、答题修订、证据、掌握度、状态重置和 state_version 已随 0004 迁移持久化；重启后可恢复测验、评分结果、Run 和证据。计划/会话仍是内存数据，Run 的结果引用不会使这些对象自动持久化。现有业务流程多数同步完成，消息响应和知识图谱发布/更新仍是占位实现；出题请求在创建事务提交后通过 FastAPI 后台任务调用 DashScope；模型不可用会将测验和 Run 标记为 failed。尚未实现可重试的独立 worker、计划/会话持久化或真实图谱发布。

资料上传和新增版本把资料、版本、片段、幂等记录、Run 与事件写入同一个 SQL 事务；任一步失败会一起回滚。相同 key 和请求在重启后返回原 material/version/run，不同内容复用 key 返回 409。旧幂等记录尚未关联 Run 时，会在首次匹配重试中补建关联，并发重试仍复用同一 Run。MySQL 默认 REPEATABLE READ 隔离级别下，上传按文件哈希加锁去重；锁冲突导致的死锁会有限重试。

创建空间与设置范围必须提供 Idempotency-Key；修改空间基础字段、画像的 PATCH 可选提供该键，提供后也保存并重放原响应。资源变化与响应快照在同一事务中提交。PATCH 和范围修改必须携带 expected_version；画像检查 profile_version，基础字段和范围检查 space_version。范围更新同时递增 scope_version，并将旧范围计划标记为 needs_replan。绑定版本在上传新版本后保持不变；范围只接受绑定版本中的实际主题。当前主题来自文本标题/片段，真实 Neo4j 图谱和有来源的前置关系尚未接入，因此不自动扩展前置主题。

删除资料会在事务中检查持久空间引用；有引用时返回 409，即使请求 cascade=true 也不能绕过。完整级联删除、删除接口的确认/版本约束及业务清理仍待实现。幂等重放还覆盖测验创建、追加答案和状态重置；交卷依据测验终态复用原 Run。其他写入接口和 24 小时幂等记录清理仍待实现。事件轮询和追加目前读取整个 Run 历史，尚未进行长对话负载优化；过期载荷在读取时清除，保留事件序号和时间用于判断续传是否过期。

测验默认使用 DashScope，需要在服务进程配置 `DASHSCOPE_API_KEY`（请勿提交密钥）；默认聊天模型为 `qwen-plus`，Embedding 保持 `text-embedding-v3`。可设置 `LEARNING_CHAT_BASE_URL` 指向 DashScope 兼容服务。测试注入固定题集或模拟 HTTP，不消费真实模型额度。模型输出通过题数、题型、难度配额、答案键及来源引用校验后才发布；这些结构校验不能保证所有题意和答案在语义上正确，仍需人工评估。

答案采用追加修订，交卷将最新答案、逐题证据、掌握度和 Run 放在同一事务内保存。未答或未可靠判定的短答为 `unverified`，不按答错计分；请求题目提示后保留教学反馈，但排除独立掌握证据。状态重置保留历史，重置前的测验与旧资料版本不写入当前分数。`MasteryPolicy` 可注入聚合窗口、阈值、间隔和复习周期；默认取最近 20 个题目族的首次有效证据，满足至少 3 族、2 次测验、24 小时间隔及应用题覆盖后才可判定掌握。默认 7 天后标记待复习，时间流逝不会降低分数。题目族归一化是文本启发式，尚不能识别所有语义改写。

评分复核暂返回明确的 `501 GRADE_REVIEW_NOT_IMPLEMENTED`，不再创建虚假的成功任务。存在测验或学习历史的空间暂拒绝删除（409），待完成可审计的级联清理。进程内后台出题不具备独立队列的故障恢复能力；服务重启后中断任务标记失败，需要新建测验。

运行学习模块回归测试（PowerShell 先展开测试文件）：

```powershell
$learningTests = Get-ChildItem .\my_agent_llms\test\test_learning_*.py -File | ForEach-Object { $_.FullName }
uv run python -m pytest @learningTests -q
```

可选 MySQL 集成测试需要已迁移的本地测试库。先设置 `$env:LEARNING_TEST_MYSQL_URL = $env:DATABASE_URL`，再运行同一测试命令；这些测试验证 Run 并发写入和接口续传，以及上传的跨应用重放、旧记录补关联、同 key 和不同 key 的并发上传；还覆盖空间同键并发更新、不同键版本冲突、旧快照下绑定新资料版本、空间创建与资料删除互斥；还覆盖测验与证据的重启恢复、同时交卷、同键原子提交、失败回滚、取消发布锁和删除保护；结束时只清理测试自身创建的数据。未设置此变量时会跳过 MySQL 用例。

---

## License

This project is open-sourced under the MIT License — see [LICENSE](LICENSE).

## Feedback

Questions or ideas? Feel free to open an [Issue](https://github.com/HHHH-LK/keel/issues) or PR.

---

如果你想要的不是"问一句答一句"的工具,而是一个**记得住、做得完、不闯祸**的长期 AI 伙伴——那就是 Keel。
