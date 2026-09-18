# 知识纠错持久化与确认发布

沿用已批准的 API/架构：纠错保存为审计事件和独立候选，不覆盖原快照；确认通过现有 graph worker 准备索引并调用 GraphService 发布。空间绑定保持不变，采纳更新另行实现。

1. 严格校验 node/relation、reject/replace、理由与来源；replace 使用受限字段补丁，禁止改写 ID/来源/版本。只接受空间当前绑定且仍为最新发布的快照，旧绑定返回版本冲突。
2. corrections context 保存来源、候选、基版本和确认记录；创建事务保存 candidate、等待确认的 Run、纠错和幂等响应。确认事务保存审核理由并入队 graph.prepare；重复确认复用 Run。
3. worker 沿用租约/重试/取消机制，准备成功后同事务调用发布、保存纠错结果并完成 Run。并发新版本使任务明确失败，不覆盖新图谱。
4. reject 保留节点/关系但标记 rejected；学习读取排除 rejected 节点。关系补丁校验端点和前置无环，重建 prerequisite 投影。新旧来源审计保持完整。
5. 创建/确认检查路径空间归属；空间删除取消未完成纠错工作并清除纠错事件，保留资料快照历史。
6. 先写内存/SQLite/MySQL 回归，再运行数据库迁移、完整后端测试及独立代码审查，最后按模块提交推送。

验收：重启持久化、幂等冲突、越界来源/目标/空间、未确认不可发布、真实版本递增、旧空间不移动、准备失败不发布、陈旧确认/worker竞争、删除/取消、迁移一致性。

代码审查发现并修复：同一资料版本的普通 reconcile 可能覆盖已发布纠错。现在复用最新 publication 的有效快照，保留纠错和审核决定；跨版本节点审核字段变化也产生 reviewed_change 冲突，保持 content_hash 的原始来源含义。学习前置关系仅投影 active 且有来源的关系。

验证结果：完整 learning 回归为 583 passed、24 skipped、1 warning，包含真实 MySQL、Neo4j、Qdrant 集成验证；Embedding 使用确定性测试实现，未调用付费模型。现有 Starlette/httpx 弃用警告仍保留。本地 MySQL 已升级至 0012_correction_context，alembic check 未检测到模型与迁移差异。
