# Web Agent / L0–L5 接入执行记录

本记录对应 [完整计划](../superpowers/plans/2026-10-24-web-agent-l0-l5-integration.md)。目标仍是所有对话入口使用通用 Agent 与完整 L0–L5，保留业务服务权威。当前仅完成测试基础设施与一项接入前范围缺陷修复，尚未实现 Web 运行时替换，不能据此宣称全量接入或性能达标。

## 执行环境与隔离

- 工作区：`C:/Users/27202/Desktop/KnowPath/.worktrees/web-agent-l0-l5`。
- 分支：`codex/web-agent-l0-l5`，基础提交 `e2f342a`。
- 主工作区已有文档、工具脚本、测试 fixture 删除及大量未跟踪文件，全部保留。新工作区另有三个 `frozen-inputs.zip` 在检出后显示行尾差异，未纳入本次改动，不自动还原。
- 解释器：主工作区的 `py/.venv/Scripts/python.exe`，Python 3.13.15；pytest 9.0.3、SQLAlchemy 2.0.54、Alembic 1.20.0。沙箱内启动器报 uv trampoline permission denied；工具自动审批允许在沙箱外启动现有解释器。此项是执行环境约束，不算业务测试失败。
- 只读迁移头检查：`ScriptDirectory.from_config(Config('alembic.ini')).get_heads()` 返回 `0021_space_scope_options`，未连接或迁移数据库。
- 启动器只继承必要 OS 环境，禁用 dotenv/插件自动加载，显式配置工作区临时 SQLite；不继承日常数据库 URL、模型密钥或自定义密钥环境变量。网络连接默认阻断；Windows asyncio 的标准库 socketpair 仅在其内部调用期间豁免。独立 MySQL 仅接受显式测试库 URL，保存结果不包含 URL。

## Task 0：基线与契约

状态：基础设施与离线基线已验证；外部 MySQL 验证未执行。提交 ID：尚未提交。

新增文件：

- `py/scripts/run_agent_checks.py`：baseline / agent / compatibility / full 套件与定向节点选择；隔离子进程与结果清单。
- `py/knowpath_backend/test/web_agent_guard.py`：测试导入前网络保护、收集/执行/跳过结果记录。
- `py/knowpath_backend/test/web_agent_support.py`：SDK 类型合法的脚本化 ChatCompletion/流式 tool_calls、假时钟/向量、真实 SQL LearningState 控制辅助器。
- `test_web_agent_checks.py`、`test_web_agent_protocol.py`、`test_web_agent_source_boundary.py`、`test_web_agent_wire_contract.py` 与合成 wire fixture。
- [兼容套件基线清单](2026-10-24-web-agent-baseline.json)：86 个文件、1,485 个收集节点，明确为仅收集，不作为执行通过结果。

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

## 接入前缺陷：原文自动全部范围

两项原有基线测试 `test_reading_source_candidates_cover_document_beyond_lesson_node[capturing/vector]` 返回 `VECTOR_UNAVAILABLE`。根因是 SpaceService 创建空间时将有效主题列表填入 `topic_ids`，同时标记 `scope_mode=all`；MessageService 原文读取却把所有非空列表视为显式白名单，丢弃未映射原文，后续检索器找不到目标。

已修改 `learning/conversations/service.py:_reading_sources`：自动全部按模式判断，不把派生列表当显式选择；排除项仍优先。新增边界测试证明，自选所有已映射主题也不能授权其他原文。该修复属于计划的来源隔离边界，不扩大到其他业务重构。无需 schema/数据迁移。

## 后续任务状态

| 任务 | 状态 / 下一项依赖 |
| --- | --- |
| Task 1 基座注入与外部提交 | 实施中；需定向测试和普通 Agent 回归 |
| Task 2 完整检查点 | 待 Task 1 接口稳定 |
| Task 3 SQL 存储 | 未实施 |
| Task 4 范围/来源依赖 | 未实施 |
| Task 5 工作副本/租约/原子发布 | 未实施，需独立 MySQL 竞争验证 |
| Task 6 RAG 证据拆分 | 未实施 |
| Task 7 模型端口/预算/工具 | 未实施 |
| Task 8 统一编排 | 未实施 |
| Task 9 全入口/Worker 切换 | 未实施 |
| Task 10 管理/删除/维护 | 未实施 |
| Task 11 backfill/修复 | 未实施 |
| Task 12 全层/故障/性能验收 | 未实施，不能用基线测试代替 A01–A38 |
| Task 13 默认切换/删除旧分支 | 未实施；R0 回滚制品也尚未构建 |

未修改日常数据库、运行配置或启动在线模型；未发布、合并或删除旧管线。
