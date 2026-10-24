# Web Agent / L0–L5 接入计划完整性与边界复核

日期：2026-10-24。

审查对象：[执行计划](../superpowers/plans/2026-10-24-web-agent-l0-l5-integration.md)。本轮仅修改计划和本审查记录，没有实施生产代码、运行数据库迁移或调用在线模型。

## 复核结论

原计划覆盖了主要模块，但有些表述仍停留在目标层面；直接实施会遇到控制命令事务、SDK 调用旁路、会话连续性、取消传播、RAG 校验拆分、回滚遗忘语义和部署队列边界不明确的问题。

累计复核已将这些缺口补为具体接口、权限、事务与发布约束，保留 14 个执行任务和 5 个里程碑，验收场景为 A01–A42，并增加设计边界到任务/验收的追踪表。以下“已修订”表示计划已补齐，**不表示对应实现已完成或测试已通过**。

## 发现与修订

优先级表示执行前需要明确的程度：P1 为可能破坏数据、恢复或业务契约的边界；P2 为实现完整性与可维护性缺口。

| 编号 | 优先级 | 原计划缺口与代码证据 | 本轮修订 | 负责任务 / 验收 |
| --- | --- | --- | --- | --- |
| R01 | P1 | 忘记/固定与回答变更一起暂存，回答失败时会撤销用户已发起的管理操作。[Agent 自动收尾](../../py/knowpath_backend/core/agent.py:109) 和原计划的统一提交不能直接承担两种事务语义 | 独立 MemoryCommandService；管理 API 立即提交、保留控制收据、撤销旧执行 token。Agent 不注册固定/遗忘写工具 | Task 5、10 / A15、A16、A25、A36 |
| R02 | P1 | 模型“端口适配”不足以阻止底层旁路：Agent [直接读取 SDK client](../../py/knowpath_backend/agents/function_call_agent.py:722)，还会在空输出时[翻倍重试](../../py/knowpath_backend/agents/function_call_agent.py:759)。当前 [Worker retry](../../py/knowpath_backend/learning/workers/model_tasks.py:176) 不能证明重试未重复计费 | 真正注入 tool-calling 端口；物理调用发送前持久预留、逻辑消息总预算跨尝试保留，unknown 调用终止、不自动重放；commit 不确定先查成功收据 | Task 1、3、5、7 / A19、A26、A31 |
| R03 | P1 | 仅以指纹作为热状态键，会在 A→B→A 时恢复旧 A 的连续上下文；当前实现本就有[范围切换后不沿用近期问答](../../py/knowpath_backend/learning/conversations/context.py:61) 的处理 | 接收时分配单调 context_epoch，指纹改变即新分段；旧 A 只可参与有范围的历史召回；区分自动全部和显式选择全部 | Task 4、9 / A07、A24 |
| R04 | P1 | [回调异常被忽略](../../py/knowpath_backend/agents/function_call_agent.py:842)，[工具超时仅停止等待](../../py/knowpath_backend/agents/function_call_agent.py:495)；recall [标为只读](../../py/knowpath_backend/tools/builtin/recall.py:15) 但管理器会更新热度 | 致命控制信号穿透；记忆工具串行；真正只读工具用独立 Session；迟到线程无可提交副本/发布权限，提交前冻结变更集 | Task 1、5、7、9 / A12、A27、A28 |
| R05 | P1 | [AnswerVerifier.answer](../../py/knowpath_backend/learning/rag/verification.py:641) 把生成、核验和修正绑在一起。若直接包装，仍是第二条生成链；若删掉整个 verifier，则会丢声明/必答要点校验 | `prepare_evidence()`、本轮 EvidenceRegistry、只检查已有候选的 `verify_candidate()`；修正回到同一 Agent；保留限定条件、依赖、partial/insufficient/clarify 语义 | Task 6、8 / A09、A19、A32 |
| R06 | P1 | 原计划说可回滚上一版，但当前[旧投影](../../py/knowpath_backend/learning/conversations/context.py:29) 没有新 tombstone 语义，回滚可能再次召回用户已忘记的原文 | 先构建带来源资格/遗忘补丁的 R0；只支持回滚 R0；回退再 backfill 也检查 tombstone，不能承诺任意旧版均兼容 | Task 4、9、11、13 / A16、A20、A29、A30 |
| R07 | P1 | 新记忆锁与原业务锁的先后顺序不明确；[SourceAccessService](../../py/knowpath_backend/learning/materials/source_access.py:84) 特意审计同一学习者跨空间共享原文，[lock_delivery](../../py/knowpath_backend/learning/materials/source_access.py:106) 有既定锁顺序，不能被记忆空间隔离缩窄 | 记忆行锁在所需业务锁之后；租约短事务不反向锁业务对象；保留跨空间及重放/流式辅助审计，reset 不撤销已交付的帮助 | Task 5、8、9 / A10、A11、A14、A23 |
| R08 | P1 | 仅写工厂/API 装配遗漏了真实启动与在途任务。[API 启动](../../py/knowpath_backend/learning/main.py:16)、[Worker CLI](../../py/knowpath_backend/learning/workers/model_cli.py:48)、[启动恢复](../../py/knowpath_backend/learning/startup.py:4) 和 [Worker 领任务](../../py/knowpath_backend/learning/workers/model_tasks.py:223) 都影响执行；单 Worker 死等空间租约会饿死前序任务 | 消息/outbox 固定运行时版本；schema/端口 readiness；停接收、排空/取消、停止旧 Worker 后切换；同空间持久序号排队，等待任务让出 Worker、不消耗模型重试 | Task 0、5、9、11、13 / A13、A14、A29、A34 |
| R09 | P2 | L4 与原消息日志都被称为“权威”；迁移收据与在线收据分类型可能重复导入同一消息 | 原消息日志为正文权威；L4 为受遗忘资格约束的副本；确定性条目 ID、正文校验和、迁移/在线共用 turn 收据，修复仍遵守 tombstone | Task 3、11 / A13、A20、A33、A37 |
| R10 | P2 | 基座[默认 hooks/管理器](../../py/knowpath_backend/core/agent.py:47)、[自动工具](../../py/knowpath_backend/core/agent.py:124)、[导入时 dotenv](../../py/knowpath_backend/core/llm.py:12) 与学习策略不匹配；[无审批回调时仍可能执行工具](../../py/knowpath_backend/agents/function_call_agent.py:590)。基座 [embedding 接口](../../py/knowpath_backend/memory/embeddings.py:24) 与 Web 批量接口也不相同 | 明确关闭代码 verify/TDD/默认 hooks/旧 honesty contract，替换受限工具；模型端口隔离配置；增加 embedding/摘要适配和 scoped conflict detector；白名单与未授权工具拒绝执行；只读状态 DTO 不含隐藏答案，L0 不自动确认画像 | Task 1、7、8 / A06、A08、A17、A18、A31、A35 |
| R11 | P2 | 计划承诺管理能力，却没定义最小 UI/API、错误和既有回复字段兼容；前端实际消费 [route/reset/completed](../../frontend/src/api.js:166) | 明确四个管理 API、revision/幂等收据、最小复用面板及错误表；保留 answer_mode/status、补充说明、citation schema/source_spans、回放和确认画像流程 | Task 9、10、12、13 / A01、A11、A25、A36、A38 |
| R12 | P2 | 早期任务引用的 DTO/变更类型原定 Task 7 才定义；路径简写和“兼容测试”范围不够明确，dry-run 也未禁止元数据写入 | Task 1 先建立中立协议与学习 DTO，禁止反向依赖；补路径约定、真实启动文件、逐项追踪；dry-run 零写入/零网络；测试启动器记录收集清单和解释器；性能目标明确为未测工程目标 | Task 0–3、11–13 / A17、A18、A21、A31、A37 |

## 第三轮复核：恢复、整轮与实施边界

本轮重新对照了 `memory/working.py:46` 的逐条驱逐、`context/engine.py:146` 的逐片段去重及预算选择、`memory/manager.py:690` 的冷恢复、`:815` 的 tick 调度、`:954` 的 L3 晋升，以及 `memory/summary.py:171` 的摘要校正。结论是：第二轮已覆盖整体架构，但不足以直接约束这些交叉路径。以下均已补入计划第 3.11 节、对应任务及 A39–A42；“补入”只表示计划修订。

| 编号 | 优先级 | 发现 | 已补充的约束 | 任务 / 验收 |
| --- | --- | --- | --- | --- |
| R13 | P1 | “整轮”仅约束 L1 驱逐，ContextEngine 去重/裁剪及 L3 单条晋升仍可能留下孤立回答；在线和 backfill 未统一配对身份 | turn_id 使用原始 LearningMessage ID；在线、迁移、冷恢复、晋升、去重和预算均使用完整合格问答原子组 | 2、5、8、11 / A03、A04、A39 |
| R14 | P1 | 检查点只存 L3 引用，却未明确新 Worker 的加载顺序和同 ID 热度/失效状态归属 | 同 revision 下先装配合格存储再解析热引用；统一身份映射、资格优先；损坏状态原子拒绝且恢复零写入 | 2、3、5 / A05、A15、A40 |
| R15 | P1 | 仅给导出加锁无法阻止 tick 在锁外增加轮次或调度 Future，可能生成半快照 | Web 强制同步 tick；通用异步模式只在静止边界导出/恢复，否则明确拒绝；调度、计数和快照约束一致 | 2、5、7 / A28、A40 |
| R16 | P1 | 来源完整性和检查点有界之间缺少具体解法；复制完整 metadata 会随历史无界增长，直接截断依赖会破坏删除传播 | 稳定依赖 ID/集合引用与事务内反向边；检查点/缓冲硬限额；flush、reconcile、remember、晋升均继承依赖，超限不得静默丢来源 | 2、3、5、7、10 / A15、A16、A41 |
| R17 | P2 | 只有固定接口，没有取消固定契约；任务局部完成也容易被误报为持久隔离完成 | pin 接受明确 pinned 布尔值；Task 1/2 集成门禁由 Task 5 关闭，M1 不宣称持久隔离/SQL 恢复已验收 | 1、2、5、10 / A25、A36、A42 |
| R18 | P1 | 串行任务编号容易被理解为可逐项部署：Task 9 早于删除/迁移闭环；Task 13 删代码后也不能仅引用 Task 12 测试结果。Task 3 的一处“SQL 原文权威”还与消息权威相冲突 | 区分开发和发布顺序；目标环境切换等待 Task 10–12；删旧代码后重验入口/业务/回滚与受影响回归；统一正文权威为 learning_messages | 3、4、9–13 / A20–A22、A29–A31、A33 |

**第三轮当时的审查范围：** 该轮限定文档审查，Task 2 当时暂停且尚未验证；这是历史状态，不代表当前执行进度。后续 Task 2 已恢复并有新增修复和测试反馈，仍需独立规格/质量审查及 SQL 集成验收。当前状态以隔离工作区执行记录及测试产物为准，不能据局部测试宣称 Web 全量切换。本次第四轮同样仅修改文档，不执行日常数据库迁移、真实模型调用或发布；保留隔离工作区原有任务勾选和执行记录。

另外收紧 R02/A26：即使物理调用已落账为 succeeded/failed，进程也可能在保存候选前停止。第一版没有逐调用持久恢复能力，因此无成功回答收据且曾有可能发送的调用时一律终止，不因账本当前已无 sent/unknown 行就允许从头重跑。

## 第四轮复核：可执行边界与验收前置

再次检查后，主要架构和业务范围无须扩大，但以下约束必须显式写入计划，避免执行者自行猜测。它们并入既有 A01–A42，不另增产品功能。

| 编号 | 优先级 | 缺口 | 本轮补充 | 任务 / 验收 |
| --- | --- | --- | --- | --- |
| R19 | P1 | R0 在 Task 4 构建，容易误用已累计新特性的分支；升级前无新表时的读取行为未定义 | 从冻结旧基线独立构建，记录补丁及制品哈希；验证 schema 升级前后能力，遗忘启用后资格读取失败拒绝放行 | 4、11、13 / A29、A30 |
| R20 | P1 | 老消息可能没有足够字段恢复范围分段，不能用当前空间配置补全历史 | 按逐消息冻结快照重建；分段不明不进入热历史，来源不明隔离；报告分别计数 | 11 / A20、A24、A39 |
| R21 | P1 | “同 ID 来源/状态一致”仍可能只比较部分字段或单向状态；Web 策略可能被构造参数降级 | 明确 source_scopes 和 superseded_by 双向核对；冲突原子拒绝，基于合格存储显式重建；整轮/来源策略不可关闭 | 2、5、7 / A31、A39–A41 |
| R22 | P2 | 已有真实 MySQL 基线阻断若不排除，会掩盖新表/锁顺序验收 | 在 Task 3/5 验收前修复 JSON confidence 往返哈希兼容及 fixture 审核决议；保留合法旧 hash 和发布审核门禁，不接受跳过代替验证 | 0、3、5、12 / A14、A21 |

同时更正审查摘要的验收数量，并将第三轮“Task 2 暂停”标为历史状态。本轮的文档检查不是上述测试通过证据；R0、新 SQL 存储、跨 Worker 原子提交、历史迁移和删除闭环仍须实际验收。

## 最终边界（第四轮确认）

1. **替换边界：** 普通聊天、原文侧栏、讲义问答的生成与对话记忆统一运行；确定性测评策略继续由程序执行。
2. **业务边界：** 出题、评分、掌握度、复习、计划、讲义/笔记生成及画像确认服务保持数据权威；本次不增加这些业务的 Agent 写工具。
3. **记忆边界：** L0–L5 全部实际运行、可恢复、可失效；来源型记忆继承依赖，空间内跨会话不等于跨资料或跨版本无条件复用。
4. **事务边界：** 自动记忆随合格回答提交；显式管理命令、调用账本和已发生交付的辅助审计独立持久，不被回答失败撤销。
5. **并发边界：** 同空间按接收序号执行、跨空间可并行；数据库锁不跨网络等待，迟到线程不能写回，致命控制信号不可吞掉。
6. **RAG 边界：** 保留检索和证据校验能力；Agent 生成的声明必须过原有强度的 checker，不能再起完整旧生成链，也不能降为只检查 citation ID。
7. **工具边界：** 模型仅有受范围约束的资料/状态/记忆能力；固定和遗忘由管理 API 执行；不注册 Shell、文件编辑或业务任意写工具。
8. **发布边界：** 先准备可回滚的 R0，再做 schema/backfill 和切换；不混跑不兼容 API/Worker，不重放不确定计费请求，不把任意历史制品视为安全回滚目标。

## 验证范围

本轮核对了计划的模块/调用链、数据权威、权限、事务、恢复、迁移、回滚及任务依赖，并对照了上表链接的实际源代码。文档静态校验覆盖任务连续编号、验收编号、追踪表、链接目标及行号、代码块和引用的既有测试文件。

尚未实施的模型端口、SQL 新表、检查点、候选 checker、R0 补丁和前端面板，其正确性仍须按计划的红绿测试、隔离 MySQL 并发验收、真实协议验证和性能测量证明。本轮没有将文档检查当成业务测试通过，也没有据此承诺已经达到性能目标。
