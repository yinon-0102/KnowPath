# Web Agent / L0–L5 接入执行记录

本记录对应 [完整计划](../superpowers/plans/2026-10-24-web-agent-l0-l5-integration.md)。目标仍是所有对话入口使用通用 Agent 与完整 L0–L5，保留业务服务权威。当前已完成测试基础设施、基座注入改造、通用热检查点/整轮策略与一项接入前范围缺陷修复，尚未实现 Web 运行时替换，不能据此宣称全量接入或性能达标。

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
| Task 2 完整检查点 | 已提交 6aadffa；独立规格/质量各 90 passed，root Agent 152 passed、兼容 1472 passed / 88 skipped；SQL 集成仍待 Task 3/5 |
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

### Task 2 质量审查闭环（提交前）

质量审查独立 probe 确认一项 Important：摘要输入自身为派生条目时，merge_sources 丢失其 source_item_ids/source_scopes。新增 flush/reconcile/direct merge 与容量/类型回滚场景，先 **12 failed / 1 passed**（`agent-vpqsdgje`），修正直接 probe 后单独 **1 failed**（`agent-t44wm4cf`），最小修复后 **13 passed**（`agent-28g8rgeu`）。现在传递三类来源数组、直接 ID 和当前范围，去重且不共享输入容器；超限/损坏在调用模型或修改摘要前拒绝。

随后的规格复审通过原 64 项，但独立确认 canonical source_scopes 的 scope payload 接受非对象/任意原 metadata。再新增 flush/reconcile、三类热层恢复、嵌套字段和合法嵌套兼容场景，**25 failed / 1 passed → 26 passed**（`agent-84zvdj7j` → `agent-6_pwsyi9`）。修复限定 scope 容器及递归 scope 字段白名单，不扩展到 Task 4 的 binding/material 领域解析；通用 raw metadata 保持原表示。

- 最终实现者指定回归 **146 passed**（`agent-du3jysxr`），全 Agent **152 passed**（`agent-e_xmq2c8`）。
- 独立规格复审 **90 passed**（`agent-bi_wobwk`），另以 probe 确认嵌套范围拒绝、恢复原子性、传递依赖及 generic 兼容，结论 spec pass。
- root 最终 `--suite agent`：**152 passed**，11.25s，`agent-nskt6onm`，退出码 0。
- root 中间兼容回归：**1472 passed / 88 skipped / 1 warning**，176.97s，`compatibility-ed018d_8`；不覆盖最后 scope 校验，不能当最终结果。源码冻结后的最终兼容回归 `compatibility-n3p7v8j2`：**1472 passed / 88 skipped / 1 既有 warning**，203.44s，退出码 0。
- 独立质量复审 **90 passed**（`agent-5tl9kr6s`），结论无剩余已确认阻断；Task 2 代码已提交 **6aadffa**。SQL 外置依赖、同 revision 合格存储恢复与完整持久隔离仍待 Task 3/5。

## Task 0 补充：真实 MySQL 基线兼容修复（已验证并提交）

提交：`14018c2`。只规范化 confidence 字段的确切整数 0/1，排除 bool，保留合法旧 float snapshot/manifest 的哈希字节；fixture 显式提交已有冲突的审核决议，不绕过发布门禁。

真实 MySQL 竞争测试另暴露 RR read-view 缺陷：空间创建已通过 current-read 取得新发布版本，普通 revision 读取却回到旧快照而报 RESOURCE_NOT_FOUND。bound_topics 在活动 UoW 中改用现有锁读取，独立只读仍为普通 SELECT，保留版本校验及既有锁顺序。

- digest 红灯：2 failed / 12 passed（`agent-ho7fi23e`），绿灯 14 passed（`agent-wll897k0`）。
- 审核 fixture 红灯为 GRAPH_CONFLICTS_PENDING（`agent-td3tsypg`）；RR race 红灯为新 revision RESOURCE_NOT_FOUND（`agent-bkt3lqu8`）。
- 实现者真实 MySQL 两文件：10 passed / 6 expected skipped（`agent-ifynxugd`）；离线相关回归 229 passed / 27 expected skipped（`agent-djl6qbrp`）。
- root 独立 digest + 两 MySQL 文件：24 passed / 6 expected skipped / 1 既有 warning（`agent-qy4lvok5`）。6 项为 Worker fixture 的 memory/sql 参数组合不适用真实 MySQL，真实 MySQL 组合已执行。
- 独立规格 21 passed（`agent-nwnhz7sl`），结论 SPEC PASS；独立质量 21 passed（`agent-ru4zzopj`），结论 QUALITY PASS。root 本轮重新读取三份结果 JSON：均 exitstatus=0、collection_errors=0，计数与以上一致。
- 专用实例再次确认：127.0.0.1:33317，PID 34088。未操作日常数据库或在线模型。四个代码/测试文件 diff --check 通过后单独提交，三个 ZIP 未包含。

## 第五轮计划复核与下一项

第五轮仅修改计划/复核记录，新增 R23–R26：数据库时间与旧租约 token 隔离、索引作业独立条件写回/就绪状态、管理命令收据优先重放/分页资格、真实模型输入固定开销。维持 Task 0–13、A01–A42；主工作区与隔离工作区静态检查均通过，文档检查不是功能验收。

Task 3 已启动单一实现者：八表、SQL/内存仓库、存储适配、依赖外置和增量迁移；要求先明确协议/失败测试，再走红绿与真实 MySQL 验收。Task 4–13 尚未实施。Task 5 仍负责关闭完整请求副本隔离与 SQL 同 revision 恢复门禁。

### Task 3 阶段证据（实施中，未审查通过）

- 首轮缺失模块/迁移红灯：22 failed / 1 skipped（`agent-4m16acsr`，无收集错误）；其中测试运行时导入失败表示模块未实现，不作为行为覆盖证据。
- 行为红灯：8 failed / 17 passed / 1 skipped（`agent-vg3foj5q`），覆盖卡片 remove 协议、调用预留、扫描限额、payload 漂移修复；后续来源/候选变更集红灯 12 failed / 17 passed / 1 skipped（`agent-m6qap17x`）。
- 首轮绿灯：34 passed / 1 skipped（`agent-l5skr6ls`，exitstatus=0、无收集错误）。包含 SQLite 与独立 MySQL 的迁移、JSON、索引、唯一/FK、回滚；跳过为 SQLite 参数下纯内存 ContextVar 测试不适用。
- root 已只读核对上述四份结果 JSON。该绿灯早于后续账本独立提交、依赖自动登记及兼容修订，不代表最终源码通过。
- 当前实现范围：八表与旧消息/会话复合约束、SQL/内存仓库、Cold/Vector/Playbook adapter、确定性 UUID、规范来源投影/未知身份隔离、依赖外置、候选状态与向量变更集。独立规格/质量审查和最终回归尚未完成，不勾选 Task 3。

独立规格审查正在闭环，已实际复现五项：跨 revision 安全收据被误判冲突；builder 为旧向量刷新代数使旧模型覆盖新模型；真实 Citation/SourceSpan 的检索版本、artifact_hash 和坐标丢失；Vector.remove 可以清除其他模型/范围的索引；snapshot 合法 JSON 标量损坏使读取抛 AttributeError 而非逻辑不可见。逐项修复必须重新审查，已有中间绿灯不覆盖这些边界。

root 中间全 Agent 回归 `agent-psmvb136` 为 191 passed / 3 skipped（无独立 MySQL 配置、SQLite 不适用纯内存 ContextVar、内存不适用 SQL 独立账本），早于上述修复，非最终证据。实现者中间兼容回归 `compatibility-4nd8hs50` 1470 passed / 88 skipped / 2 failed：原 RAG 迁移测试发现 Base 的 UniqueConstraint 与迁移 unique Index 不一致，以及离线 MySQL 迁移不应反射 MockConnection；保持原测试，分别修正声明和冻结 parent stubs，待最终重验。

### Task 3 规格审查闭环（质量审查中）

独立规格共发现六项确证缺陷，前五项见上文；最后一项是嵌套 SourceSpan 的 material_version_id 为 None/整数/空串/异版本/超长值仍可能活动投影。新增五值×双后端测试 `agent-wg7gpe26` 先 10 failed，再在 projection 入口限定非空字符串、长度及 parent 版本一致；generic 兼容不变。来源字段扩展保留真实 Citation→SourceSpan 归属关系及完整反向身份，不只保留全局去重集合或哈希。

- 最新实现者定向集 `agent-vccz2chb`：177 passed / 2 不适用 skip，含 Task 3、Task 2、原 RAG 迁移和真实 MySQL。
- 最终独立规格复审 `agent-_hvv6q7r`：170 passed / 2 不适用 skip、exit 0，结论 SPEC PASS。另有多引用跨度归属与 600 条外置依赖尾部反向查询的独立断言；SQLite 升降升已独立验证。
- 此前 root `agent-dc2pjgvo`：77 passed / 2 不适用 skip，含真实 MySQL，但早于最后 span 版本修订，不作为该修订的最终验证。
- 当前独立质量审查已开始；root 正在独立重验最终专用 MySQL 路径，完整 Agent/compatibility 回归在审查闭环后执行。尚未提交或勾选 Task 3。

### Task 3 质量修复及最终独立验证

独立质量审查确认两项 Important：内存读请求可读取另一个线程未提交并最终回滚的内容；平面 source_dependencies 坐标 span 未复用严格版本校验。单一实现者补确定性并发/来源测试，RED agent-mrqihhtf 为 14 failed / 6 不适用 skip；修复后 agent-swlvqxqp 为 191 passed / 8 不适用 skip，含独立 MySQL。

公开多步读取与 Cold/Vector/Playbook adapter 读取现在共用业务事务 RLock；返回脱离存储的快照，回滚后动态解析重新绑定的字典。ContextVar 仍负责写边界，不再被视为读隔离。坐标 span 的物料版本要求非空 str、长度不超过 128，nested 额外匹配父版本；非 span generic dependency 兼容不变，损坏读取不写库。

最终冻结源码证据：

- 独立规格 agent-ibu3tr0m：184 passed / 8 不适用 skip，SPEC PASS；覆盖仓库、Task 2 checkpoint/protocol、SQLite 迁移循环。
- 独立质量 agent-vyqj2std：98 passed / 8 不适用 skip；agent-7bwpvy5j：78 passed，QUALITY PASS。另独立验证 8 类 adapter 读取跨事务回滚阻塞、动态字典重绑和迭代器脱离存储。
- root 全 Agent + 显式独立 MySQL：agent-4k2obych，253 passed / 8 不适用 skip，48.38s。
- root 最终 compatibility-vgb5vlmn：1472 passed / 88 skipped / 1 既有依赖 warning，226.70s。88 项包括未配置的外部服务/专用 MySQL 业务组合，并非已验证；Task 3 专用 MySQL 另在上述 Agent 套件真实执行。
- root 已读取以上所有 results.json 的汇总：exitstatus=0、collection_errors=0。旧 compatibility-uoi17rzi 的原 session 已确认 terminal 1472 passed / 88 skipped，但早于最后质量修复，不用作最终证据。
- 8 项不适用 skip：SQLite 参数的纯内存 ContextVar 1 项、内存参数的 SQL 独立账本 1 项、SQLite 参数的内存线程锁 6 项；对应适用后端均执行。
- 数据库仅使用既有专用 knowpath_agent_test；未操作日常数据库、调用在线模型或部署。

第六轮文档审查已补入 R27–R30 / 3.13，主工作区及实施工作区均保留 14 任务、42 验收。Task 3 存储交付不包含 Task 4 当前来源资格、Task 5 请求副本/SQL 同 revision 恢复、Task 7/11 索引作业调度及进度、Task 10 全生命周期管理；这些仍按总计划继续实施。

Task 3 源码提交：6a72b47。暂存清单恰为上述 11 个源码/测试文件，ZIP 未纳入；提交前移除迁移文件 EOF 多余空行，git diff --cached --check 通过。下一项为 Task 4；完整目标继续保持所有 Web 入口替换，尚未完成。

## Task 4 启动与接收契约更正

Task 3 闭环文档提交 ad8f39e 后，派新的单一实现者实施 Task 4（scope.py、rollback.py、旧投影资格门禁与独立 R0）。先冻结完整 fingerprint/统一资格策略/接收事务分配协议，再红绿验证，不跳过规格与质量审查。

接收代码复核确认 MessageService.send 保留同会话 MESSAGE_IN_PROGRESS：第六轮“多个同会话请求全部先排队”的措辞会意外扩大产品能力，现已更正。实际验收为 A 完成→B 接收后取消/失败→A 再接收，仍有三个 epoch；不同会话可以使用同空间队列。allocator 独立协议可预排，但不能据此修改既有 HTTP 多在途限制。

Task 4 的全层共用资格接口需要解析外置完整依赖集合，不只检查 payload 前 512 项；纯偏好独立证明由服务端可信流程提供，不以模型可写标记豁免资料资格。实际新运行时/HTTP/Worker 接线仍由后续任务按退出门禁验收。
### Task 4 阶段结果与重新打开的验收缺口

Task 4 已提交 0fa11ba。35 项范围/协议测试通过，覆盖部分范围指纹、接收冻结、全局序号和 epoch 协议；这不是全层生产集成验收。此前独立规格/质量的 PASS 属只读审查并引用 root 测试，不能作为独立功能验证，也不足以证明本任务退出条件。Task 4 保持未完成，Task 5–13 尚未完成。

重新读取当前源码后，以下事项必须以真实仓库和业务路径的正反向测试关闭：

- 来源存在性仍由反向依赖边非空推断，不能证明资料、版本、chunk、hash 和 owner 当前有效；需要真实来源解析。
- 历史投影依赖 snapshot.source_dependencies，但生产写入闭环尚未证明；须验证合法历史可召回，再验证来源删除后不能召回，不能以永久拒绝所有历史代替资格检查。
- 同会话旧 epoch 的 recall 与近期 L1/L2/L3 需分别验证；lesson 分支重建历史可能绕过统一门禁。
- 持久投影 provenance 的 scope 与 EligibilityPolicy 所需 memory_scope 字段尚未形成一致契约；嵌套 span 的父来源继承、空间身份与指纹合法性也需集成验证。
- R0 当前仅有 manifest/read helper；尚无从冻结旧基线独立构建的制品、哈希及升级前后 schema 演练，不满足 Task 4 的独立 R0 条件。

Task 5 正在实现真实仓库租约、请求副本与原子发布。未取得最终测试结果前，不将设计、委派消息或阶段绿灯计作完成证据。

### Task 5 首批租约行为证据（中间结果）

真实 memory/SQLite 仓库测试 test_web_agent_memory_concurrency.py 已落盘。agent-7ac8ozz6 为 4 failed，缺少 acquire_execution_lease；生产仓库新增 acquire/check/renew/release 后，agent-p42k1b0q 为 4 passed、exitstatus=0、collection_errors=[]，root 已独立读取两份 results.json。覆盖同空间独占、新 UUID token、旧 token 不能释放新持有者、过期后无新持有者及已被新持有者替换均不能续回。

这不是 Task 5 最终验收。即时审计要求继续补 SQL 外层事务写门禁、消息所属空间、数据库更新时的过期判断、发布 fencing、真实 MySQL 双连接竞争和完整工作副本恢复；回答与记忆原子发布尚未验证。

### Task 5 请求副本恢复阶段验证

MemorySessionService/MemorySession 已落盘，复用通用 MemoryManager，捕获 revision/删除代数，使用请求内本地后端，freeze 导出 MemoryMutation。SQL 外层事务及真实第二空间归属测试已补齐，不再以 FK 为理由跳过该用例。root agent-8q8445q8 为 14 passed / 2 failed，准确复现恢复后 vector backend 与 cold/hot 持有不同对象；修复本地 canonical 对象绑定后，root agent-47r5cei2 为 16 passed。包含 checkpoint/protocol 的 root 定向回归 agent-0g92nle4 为 103 passed、退出码 0。上述均为隔离内存/SQLite 测试，不包含真实 MySQL 双连接竞争。

仍未关闭：按需加载而非全空间扫描、真实资格 resolver 和生产来源闭环、配置在读存储前拒绝、候选所有层变更集、SQL 重启后的整轮晋升、发布 fencing、回答/记忆/审计/事件原子提交、成功收据重放和 Worker 队列。Task 5 未完成，源码尚未提交或进入最终独立双阶段审查。

生产接线阶段新增 MessageService 正常发布与 Worker 等待预算测试。root agent-8ob6yk53 为 18 passed / 2 failed / 1 MySQL opt-in skipped，准确复现回答成功但缺少 turn 收据；接入发布事务后，root agent-5u9f01ir 为 20 passed / 2 failed / 2 MySQL opt-in skipped，失败转为 Worker 等待仍消耗 attempt。继续修复后 root agent-ezlsdeay 为 24 passed / 3 MySQL opt-in skipped、退出码 0。该阶段仍不证明 claim/generate 竞态、真实来源资格、续约、MySQL 锁序或完整失败回滚；必须继续验收。Worker._locked 已预取业务/outbox/Run 锁，后续重复读取不自动等于反向新取锁，最终以实际 SQL 锁表与双连接测试判断。

持久投影补入冻结 memory_scope，生产 resolver 开始读取真实 material/version/chunk，不再把反向依赖边存在当来源有效。root agent-cwvr9fde 对 commit/concurrency/scope 三文件回归为 40 passed / 3 MySQL opt-in skipped、退出码 0。普通来源正向已验证；检索 artifact/hash/span、空间授权和派生依赖递归资格仍未完整验证。此结果不关闭 Task 4 或 Task 5。

扩大既有 Worker 回归 agent-xrppl0rm 得到 132 passed / 9 failed：outbox 与空间租约时钟不一致导致接管受阻、删除遗漏 memory_scope 引用、空间删除后 release 抛错。修复后 root 相同三文件 agent-g9jz21nm 为 141 passed。完整离线 Agent 中间回归 agent-43w5g0fc 为 296 passed / 14 skipped；其中 6 项要求显式 MySQL 配置，其余为后端不适用。

完整兼容回归 compatibility-8r6ut2ok 为 1449 passed / 23 failed / 88 skipped、退出码 1，当前仍是阻断：多轮续聊 restore_checkpoint 检出冷热来源身份不一致，部分历史 recall 为空，bounded source retry 未正常重领。必须保留严格身份校验和现有业务断言，修复生产元数据/时间一致性及历史来源闭环后重验。不能引用 Agent 阶段绿灯宣称 Task 5 或整体兼容通过；源码未最终验收。

兼容修复中的 root 定向复验 agent-6kpx_glr 为 133 passed / 21 failed / 49 skipped；后续 context/adaptive 两文件 agent-js_0pdtn 为 58 passed / 6 failed / 28 MySQL opt-in skipped。memory 历史召回正向已恢复；SQL 仍在合并 last_access 时出现 aware/naive TypeError，bounded source retry 两后端仍未正常重领。该中间结果只用于定位后续修复，不替代完整兼容复验。

上述兼容阻断随后已修复：冷热条目的权威创建时间与完整 provenance 对齐，created_at/last_access 按 UTC 比较；业务 uid 与租约执行身份分离；prompt_data 同时从实际模型输入和预算中剥离服务端 memory_scope/source_dependencies。root 定向 agent-ccmpcyzi 为 64 passed / 28 MySQL opt-in skipped。root 完整兼容复验 compatibility-pof574u4 为 1472 passed / 88 skipped / 1 既有 warning，168.55s，exitstatus=0、collection_errors=0，关闭前述 23 项兼容失败。

root 已独立读取实现者专用 MySQL 正常入口结果 agent-1pcwzs3v：6 passed，无 skip；故障验证 agent-wmyfob3c：4 passed / 2 MySQL opt-in skipped，均 exitstatus=0、collection_errors=0。后者覆盖外层发布事务失败与提交成功后连接异常的收据恢复；并非真实双连接竞争证明。完整 Task 5 仍待双连接锁序/fencing、按需恢复、检索 artifact/hash/span、派生依赖资格等门禁，源码尚未进入最终独立规格/质量审查。

### Task 5 按需恢复、派生来源及发布边界复核（仍在实施）

请求副本已改为启动加载 checkpoint 引用及完整 turn，历史 cold/vector 按需加载；资格检查移到 memory 短事务之外，避免持有 memory 锁再取 material 锁。上述为当前实现状态，不代表全部退出门禁通过。

root 重新读取以下隔离结果，确认计数与退出状态；均无收集错误：

- agent-k_4fy4uq：135 passed / 9 skipped、exitstatus=0。checkpoint 时间按 UTC 比较，但保留原 datetime 表示，修复无条件规范化破坏 round-trip 的问题。
- agent-ori4nwi9：128 passed / 11 skipped、exitstatus=0。多轮摘要保留贡献来源；同 epoch 不同 receive_sequence 不再误判身份不同，混合 epoch 的祖先不能被后续来源洗掉。
- agent-hgd9nwu2：12 passed / 4 后端不适用 skipped、exitstatus=0。专用 MySQL 双连接验证旧 token 在无替代持有者及有替代持有者时均不能发布；不覆盖共享资料发布、评分、删除竞争。
- agent-antx867l：135 passed / 2 failed / 12 skipped、exitstatus=1。真实 item/message 的递归来源资格仍有缺口：依赖来自旧 material scope，候选声明当前 general scope 仍被接受。两个失败均为 test_transitive_dependency_requalifies_real_item_and_message_scope 的真实后端参数，当前属于阻断。

本轮 root 对当前工作树重新运行 commit/checkpoint/protocol：agent-f5f515xd 为 135 passed / 2 failed / 12 skipped、退出码 1，原 session 95430 已确认终止；重复确认上述两个来源资格失败仍在，不能按已修复处理。专用 MySQL 未由 root 占用。

本轮只读审计另确认：_publish_answer_once 的来源锁目前仅覆盖 bindings 与 snapshot.sources，尚未包含完整 consumed recall/cards 依赖；无 delivery_plan 路径还在来源锁之前读取锁定 message。需要先收集完整来源闭包，发布按 assessment→space→material/version→message→outbox→Run→memory 顺序锁定并再验资格，不得在末尾 session.publish 内反向取得业务锁。已交唯一实现者继续修复；Task 4/5 保持未完成，Task 6–13 与独立 R0 门禁继续保留。
