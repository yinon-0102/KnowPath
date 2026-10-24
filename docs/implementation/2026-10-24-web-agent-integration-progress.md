# Web Agent / L0–L5 接入执行记录

本记录对应 [完整计划](../superpowers/plans/2026-10-24-web-agent-l0-l5-integration.md)。目标仍是所有对话入口使用通用 Agent 与完整 L0–L5，保留业务服务权威。当前已完成测试基础设施、基座注入改造与一项接入前范围缺陷修复，尚未实现 Web 运行时替换，不能据此宣称全量接入或性能达标。

## 执行环境与隔离

- 工作区：`C:/Users/27202/Desktop/KnowPath/.worktrees/web-agent-l0-l5`。
- 分支：`codex/web-agent-l0-l5`，基础提交 `e2f342a`。
- 主工作区已有文档、工具脚本、测试 fixture 删除及大量未跟踪文件，全部保留。新工作区另有三个 `frozen-inputs.zip` 在检出后显示行尾差异，未纳入本次改动，不自动还原。
- 解释器：主工作区的 `py/.venv/Scripts/python.exe`，Python 3.13.15；pytest 9.0.3、SQLAlchemy 2.0.54、Alembic 1.20.0。沙箱内启动器报 uv trampoline permission denied；工具自动审批允许在沙箱外启动现有解释器。此项是执行环境约束，不算业务测试失败。
- 只读迁移头检查：`ScriptDirectory.from_config(Config('alembic.ini')).get_heads()` 返回 `0021_space_scope_options`，未连接或迁移数据库。
- 启动器只继承必要 OS 环境，禁用 dotenv/插件自动加载，显式配置工作区临时 SQLite；不继承日常数据库 URL、模型密钥或自定义密钥环境变量。网络连接默认阻断；Windows asyncio 的标准库 socketpair 仅在其内部调用期间豁免。独立 MySQL 仅接受显式测试库 URL，保存结果不包含 URL。

## Task 0：基线与契约

状态：基础设施与离线基线已验证；外部 MySQL 验证未执行。提交 ID：`c36bc6d`（`test(agent): establish isolated Web integration baseline`）。

新增文件：

- `py/scripts/run_agent_checks.py`：baseline / agent / compatibility / full 套件与定向节点选择；隔离子进程与结果清单。
- `py/knowpath_backend/test/web_agent_guard.py`：测试导入前网络保护、收集/执行/跳过结果记录。
- `py/knowpath_backend/test/web_agent_support.py`：SDK 类型合法的脚本化 ChatCompletion/流式 tool_calls、假时钟/向量、真实 SQL LearningState 控制辅助器。
- `test_web_agent_checks.py`、`test_web_agent_protocol.py`、`test_web_agent_source_boundary.py`、`test_web_agent_wire_contract.py` 与合成 wire fixture。
- [兼容套件基线清单](2026-10-24-web-agent-baseline.json)：初次 86 个文件、1,485 个收集节点，Task 1 扩展结果见下文；明确为仅收集，不作为执行通过结果。

红绿证据：

1. 新启动器测试在未实现时为 8 failed，原因是入口/保护模块缺失；首轮还发现系统默认 pytest 临时目录无权限，改用工作区内独立临时目录后确认实际红灯。
2. 初版网络保护误拦截 Windows asyncio socketpair；单独 asyncio 测试复现后改为 thread-local 内部豁免，不开放任意 localhost 端口。
3. 审查发现显式 MySQL hostname 与连接后的 IP 不一致，以及 full 套件未递归收集；新增测试先得到 2 failed / 8 passed，再解析显式主机的允许地址并使用递归收集修复。
4. 使用合成资料通过真实业务入口验证三类入口 × 三种回答模式的 9 个协议组合：响应、route/delta/completed 顺序、引用身份、历史入口标识；全部通过。
5. 合法 SDK 流式帧经过真实 Agent parser、tool loop 和 finalizer，未替换私有执行方法。SQL replay/restart/cancel/delete/scope 控制使用真实仓库。尚不存在的 L0–L5 状态查询、记忆租约和 backfill 操作明确报未实现，分别在 Task 3/7、5、11 补齐，不制造状态。

实际命令均在隔离工作区 `py/` 下运行，以现有解释器绝对路径替代计划的相对 `.venv` 路径：

| 命令参数 | 实际结果 | 结果目录 |
| --- | --- | --- |
| `scripts/run_agent_checks.py --baseline`（修复前） | 224 passed / 49 skipped / 2 failed | `baseline-d3c_szjo` |
| `--test .../test_web_agent_source_boundary.py`（修复前） | 1 failed，自动全部错误排除未映射原文 | `agent-o8em76hw` |
| `--test .../test_web_agent_source_boundary.py --test .../test_reading_messages.py` | 28 passed / 3 skipped | `agent-r9e4wot8` |
| `scripts/run_agent_checks.py --baseline`（修复后） | **226 passed / 49 skipped**，52.84s | `baseline-8me78w8n` |
| `--suite compatibility --collect-only` | **1,485 collected / 0 collection errors**，8s | `compatibility-ai4pu736` |
| 四个新增 Task 0 测试文件，逐个 `--test` | **27 passed**，9.54s | `agent-jrf8qaz4` |

结果目录均位于工作区 `.runtime.local/agent-checks/`，包含 `selection.json` 与 `results.json`，后者区分 collect_only 和执行阶段，逐项记录跳过原因。49 项跳过来自独立 MySQL 未配置或 MySQL 事务调度要求，不属于已通过项目。唯一已见依赖警告为 Starlette TestClient 的 httpx 弃用提示。

## 独立 MySQL 验收环境补充（2026-10-24）

已在隔离工作区 `.runtime.local/agent-mysql-test/` 初始化独立 MySQL 5.7.43 实例，仅监听 `127.0.0.1:33317`，使用专用 `knowpath_agent_test` 数据库和测试用户；不使用日常 MySQL 服务、数据目录或凭据。现有 schema 已在此库从空库升级到 `0021_space_scope_options`。这是测试环境准备，不是日常数据库迁移。

通过隔离启动器显式 `--mysql-url` 执行 `test_learning_spaces_mysql.py` 与 `test_learning_model_tasks_mysql.py`，结果目录 `agent-69a_f52_`：**3 passed / 6 skipped / 7 setup errors**。跳过的是 Worker 文件的 memory/sql 组合，真实 MySQL Worker 组合通过；7 个空间用例在资料准备阶段未完成，尚未进入其并发断言。正在定位具体异常，不能将此结果记为 MySQL 总体验收通过。后续新表与竞争验收仍须单独执行。

只读诊断已确认：MySQL 5.7 JSON 将节点 `confidence: 1.0` 读回为整数 `1`，旧 JSON 字节哈希不一致，GraphWorker 在 PreparedBackend 调用前拒绝不可变快照。临时 runtime monkeypatch 仅将 schema 的 confidence 整数 0/1 恢复为浮点、保留合法旧 hash，验证 **9 passed / 6 expected skipped / 1 failed**；这不是已交付的源码修复。剩余一项旧空间 fixture 改标题后仍传空审核决议，被正确拒绝为 `GRAPH_CONFLICTS_PENDING`，须修 fixture 显式审核冲突，不放宽生产门禁。跟踪源码修复排在 Task 2 审查通过之后。

## 接入前缺陷：原文自动全部范围（详细）

两项原有基线测试 `test_reading_source_candidates_cover_document_beyond_lesson_node[capturing/vector]` 返回 `VECTOR_UNAVAILABLE`。根因是 SpaceService 创建空间时将有效主题列表填入 `topic_ids`，同时标记 `scope_mode=all`；MessageService 原文读取却把所有非空列表视为显式白名单，丢弃未映射原文，后续检索器找不到目标。

已修改 `learning/conversations/service.py:_reading_sources`：自动全部按模式判断，不把派生列表当显式选择；排除项仍优先。新增边界测试证明，自选所有已映射主题也不能授权其他原文。该修复属于计划的来源隔离边界，不扩大到其他业务重构。无需 schema/数据迁移。

## 后续任务状态

Task 1 第一版经规格和质量两轮独立审查，发现并修复：hook 将有效回答改为空白；取消信号重复取样造成成功返回/自动写入；默认召回 hook 绕过模型端口；严格模式下旧 verify/TDD 回调吞异常；默认 SDK 端口取消行为与旧中断字符串契约不一致。严格策略默认 hooks 为空，构造时拒绝旧 verify/TDD；无效配置在创建记忆存储之前拒绝。保留普通调用者的默认 hooks 和中断文本。最后一项构造器检查补测为 2 failed → 2 passed。

Task 1 代码提交：`a3879e5`（`refactor(agent): inject memory and support externally committed turns`）。交付 `core/turn.py`、`core/model_port.py`、`memory/contracts.py`、`learning/agent_runtime/contracts.py`；修改 Agent 构造与收尾、Function Calling 调用循环、MemoryManager 卡片存储注入。未给通用模块引入 learning 反向依赖；调用方注入的记忆和 SDK 客户端由调用方关闭。检查点契约目前只是版本化数据类型，完整导出/恢复属于 Task 2。

独立执行结果（修复后的最终版本）：

- 全部 `test_web_agent_*.py` 自动收集并执行：**62 passed**，9.98s，`agent-yvcgztqh`（35 项 Task 1 + 27 项 Task 0）。
- Task 1 指定回归加 Simple/ReAct/Reflection/TDD/Playbook/KG-hook 调用者：**117 passed**，5.46s，`agent-9yxp8n9d`，其中包含 35 项 Task 1 新测试。
- `test_package_installation.py`：**9 passed**，7.39s，`agent-stp15r66`；覆盖包导入、维护入口和迁移模块加载，不等于最终 wheel 验收。
- 扩展 compatibility 套件至 93 个文件（补齐共享基类调用者与包导入），仅收集 **1,560** 项，0 collection errors，`compatibility-bsckk4gv`；[清单](2026-10-24-web-agent-baseline.json) 同时保留原 86/1,485 基线摘要。尚未执行完整兼容套件。

Task 1 的 external 只控制自动收尾。其“持久 L0–L5 不变”的广义退出门禁必须在 Task 5 请求隔离工作副本接通后验证；不得把当前空历史测试作为完整持久化隔离证据。默认 hooks、verify/TDD 的 Web 关闭/适配约束也需由后续工厂检查，不能凭单个 port 测试宣称所有外部模型调用已受预算控制。

| 任务 | 状态 / 下一项依赖 |
| --- | --- |
| Task 1 基座注入与外部提交 | 接口与定向回归已验证；持久隔离退出门禁待 Task 5 |
| Task 2 完整检查点 | 最终 Agent 113 项通过，独立规格复审 51 项通过；质量审查中，未提交，SQL 集成仍待 Task 3/5 |
| Task 3 SQL 存储 | 未实施 |
| Task 4 范围/来源依赖 | 未实施 |
| Task 5 工作副本/租约/原子发布 | 未实施，需独立 MySQL 竞争验证 |
| Task 6 RAG 证据拆分 | 未实施 |
| Task 7 模型端口/预算/工具 | 未实施 |
| Task 8 统一编排 | 未实施 |
| Task 9 全入口/Worker 切换 | 未实施 |
| Task 10 管理/删除/维护 | 未实施 |
| Task 11 backfill/修复 | 未实施 |
| Task 12 全层/故障/性能验收 | 未实施，不能用基线测试代替 A01–A42 |
| Task 13 默认切换/删除旧分支 | 未实施；R0 回滚制品也尚未构建 |

未修改日常数据库、运行配置或启动在线模型；未发布、合并或删除旧管线。

## Task 2：检查点与整轮策略执行证据（实施中）

首轮实现新增热检查点、canonical 来源依赖和 Web 上下文策略，并修改 L1/L2/L3、SemanticIndex 及 ContextEngine。热检查点不复制 L0/L4/L5 或向量；包含有限 JSON/版本校验、容量约束和 async tick 静止边界。Web 数据包装保留 `source_role`，同 ID 恢复校验包含轮次、来源身份和当前 superseded 状态。

- root 独立执行全部新 Agent 测试：**89 passed**，12.27s，`agent-aj1kvsao`；此运行早于最后两项构造门禁补测，不作为之后所有修改的最终结果。
- 实现者最后一轮定向与计划指定旧测试：**85 passed**，`agent-_plveubx`，其中 Task 2 新测试 29 项。
- 实现者 compatibility：**1472 passed / 88 skipped / 1 warning**，188.97s，`compatibility-z0nozy1d`。跳过来自未显式配置的外部 MySQL/Neo4j/Qdrant/真实模型条件，warning 为既有 Starlette/httpx 弃用提示；此套件没有使用本轮单独建立的 MySQL URL。

独立规格审查实际复现以下缺陷，均在修复前保留未验收：L2 buffer 与 L1 的同 ID 内容漂移/对象分叉；Web 猜配对和不完整轮进入近期上下文；组选择绕过跨层去重；默认 generic 无来源摘要在第 513 条输入因新依赖限额失败；自动 L0 晋升复制完整 metadata。必须新增失败用例并修复，然后重新审查与定向验证；上述绿灯不能覆盖这些未测试边界。

上述五项已集中新增回归并确认红灯：**13 failed / 29 passed**，`agent-a9x1jvin`。其中也覆盖 Web remember 的 metadata 约束，以及无效整轮的冷恢复/晋升。当时尚无该轮绿灯；后续修复与复审证据见下节，不将此前 compatibility 结果当作这些新增用例的通过证明。

SQL 合格集合、外置依赖、同 revision 恢复以及完整 request-local 持久隔离仍归 Task 3/5，尚未证明。

### Task 2 修复与独立复核（最新）

首轮五项已修复。新增 Web 构造门禁用例 **2 failed → 2 passed**，禁止显式关闭 whole_turns/provenance；追加生命周期与来源身份用例 **2 failed → 通过**，任一方向 superseded_by 冲突均拒绝，source_identity 纳入 source_scopes。

- 实现者最终定向及指定回归：**107 passed**，`agent-ncwoh71r`；全 Agent：**113 passed**，`agent-lzww__lm`。
- root 在本轮重新运行 `scripts/run_agent_checks.py --suite agent`：**113 passed**，10.45s，`agent-2_5m5ohg`，退出码 0。
- 独立规格复审者运行检查点和上下文测试：**51 passed**，`agent-ax2i4un2`；另以最小复现确认双向生命周期与 source_scopes 冲突抛出 InvalidCheckpoint，结论为 Task 2 spec pass。
- 兼容套件 `compatibility-lkoz10us`：**1472 passed / 88 skipped / 1 既有 warning**，213.50s，退出码 0。此运行覆盖首轮五项修复，最后构造 guard/身份微改由最终 113 项套件覆盖；未将此套件标为覆盖所有最后修改。root 已只读核实 JSON 结果产物的计数、0 收集错误及退出状态。
- 当前正在独立代码质量审查；未提交 Task 2，未宣称完整持久恢复或 Web 切换完成。

第四轮计划复核已同步主工作区及隔离工作区，保留已完成勾选；补充独立 R0 制品、旧历史范围分段、来源身份和 MySQL 基线门禁。此轮文档静态验证为 14 个任务、42 个连续验收编号、22 项审查发现，非功能验收证据。

专用 MySQL 测试实例本轮重新确认存活：PID 34088，mysqld 路径与独立实例相符，127.0.0.1:33317 处于监听；不重启或操作日常 MySQL 服务。
