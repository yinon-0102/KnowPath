# RAG 第一批基础实现记录

日期：2026-09-20。分支：`codex/rag-foundation`。工作区：`C:/Users/27202/Desktop/KnowPath/.worktrees/rag-foundation`。

## 已实现与验证

- 不可变来源跨度、学习范围、候选、插件输入输出、引用 v2 和回答/支持性状态契约。
- 范围身份保留资料版本、学习图谱版本与空间范围版本；允许区间并集必须完整覆盖新块全部跨度，跨版本/提取 hash/page/block 不得混用。
- `SpaceService.rag_scope_snapshot()` 从现有权威图谱与旧 chunk 生成原文范围快照，处理 topic 别名和排除关系。
- `SourceAccessService.consult_citation()` 兼容旧引用并解析新引用；回读原文，不信任候选正文；新引用查阅映射到旧身份，保留活动/待生成测验的辅助答题审计。
- 追加迁移 `0014_rag_snapshots`，9 张表与 SQLAlchemy 元数据一致；仓储支持不可变重放、版本/资料外键约束、原文跨度与范围映射。
- SQL 资料/空间删除接入同事务派生记录清理；空间删除保留共享内容索引；失败整体回滚。MySQL 可重复读下清理使用当前读取，范围写入按顺序锁定资料版本，避免空范围快照在资料删除后提交。
- 10 道合成受控题、8 个互不泄漏的问题家族、34 个校验过的原文跨度、3 个服务故障夹具。所有人工复核标签仍为 pending。

实现对原计划的文件划分细化：新增 `learning/rag/sources.py` 专门解析引用；9 张表放在 `learning/persistence/rag_models.py` 并由 `db.py` 注册；删除清理放在 `rag_cleanup.py`，通过现有持久化删除入口调用。避免把解析、存储和删除职责堆入对话 service。

## 验证证据

最终相关回归：**188 passed, 18 skipped, 1 warning**。其中 3 项为真实 MySQL 集成：从 0013 升级保留旧来源并完成读写/删除、旧事务视图下清理后来提交的范围、资料删除后阻止空范围快照提交。

所有 MySQL 验证使用本机服务中新建的唯一 `knowpath_rag_test_<uuid>` 数据库，测试 finally 自动清理；现有业务库未迁移。18 项跳过来自旧测试的额外 MySQL URL 开关及不适用的内存持久化变体，不视作通过。唯一 warning 为既有 Starlette/httpx 弃用提醒。

从工作区 `py` 目录运行：

```powershell
$regressionFiles = @('knowpath_backend/test/test_learning_migrations.py', 'knowpath_backend/test/test_learning_source_access.py', 'knowpath_backend/test/test_learning_graph_scope_contract.py', 'knowpath_backend/test/test_learning_message_retrieval.py', 'knowpath_backend/test/test_learning_material_deletion.py', 'knowpath_backend/test/test_learning_space_deletion.py', 'knowpath_backend/test/test_learning_storage.py', 'knowpath_backend/test/test_learning_raw_materials.py')
$ragFiles = @(Get-ChildItem knowpath_backend/test/test_rag_*.py | ForEach-Object { $_.FullName })
uv run pytest @ragFiles @regressionFiles -q -p no:cacheprovider --basetemp .pytest-rag-final
```

真实 MySQL 测试需通过环境变量 `LEARNING_TEST_MYSQL_ADMIN_URL` 显式提供本机测试管理员连接，凭据不保存在计划中。本次实际使用根工作区已有 Python 虚拟环境运行，避免隔离工作区重新安装依赖。

从工作区根目录运行 `python docs/research/tree-rag/evaluation/build_fixtures.py --check`，输出 `Validated 10 synthetic questions, 8 isolated families, 34 source spans, and 3 fault specifications.`。代码与迁移已完成独立复审；最终回归包含复审后的并发修正。

## 尚未完成的工作

第一批是可验证基础，不等于全链路迁移完成。当前原文坐标桥接锚定不可变的旧解析 chunk；第二批新解析器必须验证新块到这些原文坐标的映射。尚未运行真实资料批量回填或切换任何在线检索路径。

下一步按批次②实施解析、语义分块、跨页来源映射与索引构建/发布。Manifest/publication 目前仅有表结构；readiness、发布切换、失败恢复、外部索引删除和缓存清理尚未实现。现有图谱向量身份及 `text[:6000]` 保留，待第二批输入上限与分块路径就绪后一起迁移。

之后依次实施普通 BM25＋向量＋RRF＋重排、上下文证据组、生成/修正/再次核验，再实施 B1 并运行真实模型对照。60 道真实资料题的人工证据核验、模型/费用/延迟配置冻结与保留集评测均未完成；不能据此判断树形方案收益。
