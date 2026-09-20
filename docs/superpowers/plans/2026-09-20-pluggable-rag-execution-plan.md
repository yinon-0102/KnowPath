# KnowPath 可插拔 RAG 工程执行计划 v1.1

> **For agentic workers:** 使用 `executing-plans` 按批次执行；若用户选择委派，再使用 `subagent-driven-development`。以下复选框用于记录实际完成情况。

**Goal:** 将已确认的普通 RAG 与树形 B1 设计落实到数据契约、迁移、模块、验证和真实模型对照评测。

**Architecture:** 先完成共同原文与范围映射、独立内容索引和普通混合 RAG，再增加有界结构扩展。两插件共享重排、上下文组装、生成和核验，通过固定快照的配对实验选择默认方案。

**Tech Stack:** Python 3.13、Pydantic、SQLAlchemy/Alembic、MySQL、Qdrant、pytest，复用现有模型 provider、Run 与发布保护机制。

**授权与状态：** 用户随后已授权持续实施。A/B1、共享回答链路、迁移、发布恢复和评测工具已落地，已完成80次真实开发请求（30次正常返回、50次失败）。默认配置未通过验收，后续预算诊断与修复单独记录；人工标签、费用/延迟门槛和保留集验收仍未完成。以下按实际证据更新，操作说明见 `docs/research/tree-rag/OPERATIONS.md`，结果见[实施记录](../../research/tree-rag/IMPLEMENTATION-STATUS.md)。总体行为边界见[整体设计](2026-09-20-pluggable-rag-discussion-plan.md)第 10.9 节。本计划细化首版执行规则，不扩大到 B2/B3、自动路由或联网补知识。

---

## 1. 执行基线与文件职责

设计时核对的旧路径：`learning/rag/retrieval.py` 的 `point_id` 包含 `graph_version`；`learning/rag/indexing.py` 固定图版本 1；该文件与 `learning/conversations/service.py`、`learning/knowledge/preparation.py` 曾存在 `text[:6000]`。本次保留旧身份适配器，新增独立检索身份与范围映射，三处静默截断已替换为完整证据和显式预算检查；不能仅删字段或截断表达式就认为完成隔离。

下表路径以 `py/knowpath_backend/` 为前缀；迁移、评测资料另列完整仓库相对路径。新增文件明确标为“新增”，现有文件只调整职责交接，不进行无关重构。

| 文件 | 动作与职责 |
|---|---|
| `learning/rag/contracts.py` | 新增：不可变来源跨度、范围句柄、候选、预算、插件协议、回答/核验状态 |
| `learning/rag/scope.py` | 新增：旧图谱来源到原文区间、新 chunk 的映射，完整包含检查 |
| `learning/rag/parsing.py`、`chunking.py` | 新增：结构提取、语义单元、跨页合并与超长拆分 |
| `learning/rag/lifecycle.py` | 新增：构建清单、readiness、发布、重试、回滚与删除 |
| `learning/persistence/rag_repository.py` | 新增：检索版本、结构、映射、清单持久化 |
| `learning/rag/bm25.py`、`fusion.py`、`plugins.py` | 新增：关键词通道、RRF、普通插件与注册表 |
| `learning/rag/reranking.py`、`context.py`、`verification.py`、`pipeline.py` | 新增：公共重排、证据组选择、逐结论核验、流程编排 |
| `learning/rag/tree.py` | 新增：B1 有界扩展与 trace |
| `learning/rag/cli.py` | 新增：build / validate / publish / rollback 操作入口 |
| `learning/materials/service.py`、`workers/material_ingest.py` | 修改：解析接入；旧原文及引用保持可读取 |
| `learning/persistence/db.py` | 修改：追加 ORM 定义，与迁移保持一致 |
| `learning/rag/retrieval.py`、`rag/indexing.py` | 修改：保留旧适配器，增加基于检索版本的新身份与索引入口 |
| `learning/knowledge/preparation.py`、`spaces/service.py` | 修改：建立范围快照映射，保留图谱语义身份 |
| `learning/conversations/service.py`、`context.py`、`generation.py`、`schemas.py` | 修改：调用公共 pipeline、持久化状态与新来源引用；保留活动测验分支 |
| `learning/materials/source_access.py`、`materials/deletion.py`、`spaces/deletion.py` | 修改：新旧引用解析、测验查阅审计与删除清理 |
| `learning/config.py`、`providers/models.py`、`api/contract.py` | 修改：配置 profile、真实服务适配、响应契约 |
| `py/migrations/versions/0014_rag_snapshots.py` | 新增：追加表；当前检查到的前置版本为 `0013_material_raw`，实施前再核对 heads |
| `py/knowpath_backend/rag_eval/` | 新增：`__init__.py`、`cli.py`、`dataset.py`、`runner.py`、`scoring.py`，负责冻结、运行、评分、配对报告 |
| `docs/research/tree-rag/evaluation/` | 新增：题集、评分规则、冻结配置与运行报告；不存凭据 |

## 2. 首版算法执行规则

### 2.1 语义单元和扩展触发

1. 法规保持条款，教材保持概念与解释，《论语》保持章句；跨页续文合并为一个语义单元，并保存多个原文跨度。标题层级不可靠时使用带质量标识的原文窗口。
2. 不设强制最小长度；超过 embedding 或生成输入上限时按款、句或公式/代码边界拆分，保存 `continuation_of` 和明确的条件/例外关系。不能静默截断。结构规则优先；embedding 或模型辅助分块只有在开发集显示必要时引入并记录 profile。
3. B1 先使用 A 的同一混合召回排名。只在种子有真实小节归属、相邻续文或可追溯条件/例外关系时扩展；无结构依据不猜父节点。
4. 扩展限定在同一资料版本、同一允许小节的直接相关叶子中，不递归加载祖先整章。候选优先级为明确条件/例外关系、续文关系、问题相关的直接相邻叶子；同级按种子排名及原文顺序稳定排序。仅仅共父节点不足以收录所有后代。
5. 首轮 profile：A 最多 40 个重排候选；B1 取最多 30 个种子及最多 10 个去重扩展，扩展不足从剩余普通排名补足至最多 40。每父节点最多扩展 3 个；这些数值均是开发集实验起点，不是最优值。基础两路召回上限另记配置并保持两组相同。
6. 所有扩展先做范围与版本检查，去重按检索版本内的 chunk 身份；超预算停止。记录触发种子、父节点、关系依据、拒绝原因和实际额外读取量。候选不足时保留实际数量，不制造占位候选。

参考算法（实现时落入 `tree.py`，`allowed` 使用第 3 节完整包含规则）：

```python
def bounded_candidates(ranked, expansions, allowed):
    base = list(dict.fromkeys(cid for cid in ranked if allowed(cid)))
    selected = base[:30]
    added = 0
    per_parent = {}
    for parent, cid in expansions:  # 按上述关系、种子排名与原文顺序排序
        if added >= 10:
            break
        if cid in selected or not allowed(cid) or per_parent.get(parent, 0) >= 3:
            continue
        selected.append(cid)
        per_parent[parent] = per_parent.get(parent, 0) + 1
        added += 1
    selected.extend(cid for cid in base if cid not in selected)
    return selected[:40]
```

### 2.2 证据组、充分性与回答核验

- A/B1 均使用公共证据组装器。已知结论与必要条件形成不可拆分证据组；上下文按组计算 token，不能仅保留结论而裁掉必要条件。普通组装器不借此追加预算外检索；组内证据缺失时将该结论标为不可完整回答。结构关系与核验不能保证发现所有隐含例外，需报告漏检。
- 问题拆成所需答案要点；每项记录证据跨度、支持/不支持/无法判定。运行时需求分析与评测人工金标分离，模型不能读取金标。相似度不是充分性的判定阈值。
- 对完整性问题另检查覆盖范围；Top-K 没找到不是“全文没有”。回答状态为 `answered`、`partial`、`insufficient`、`clarify`；服务失败为 `failed`，不能混同证据不足。
- 生成后逐条核验直接事实、推导前提和明确标注的示例，输出 `supported / unsupported / undetermined`、`claim_id`、`source_spans`、原因及缺失要点。规则验证身份、范围和数字等确定性项，模型辅助核验语义；重要样本人工复核。
- 允许一次回答修正，并必须再次核验；首版生成最多 2 次、核验最多 2 次。仍不通过的独立句可删除，整组依赖结论一并删除；仅发布已有支持的原样内容，不能在删除阶段再生成未经核验的新措辞。缺少关键要点则返回部分回答或证据不足。
- 核验调用错误、超时或结构化结果无效，返回明确失败，不当成 `undetermined` 或无答案。所有调用受总 deadline 和 token/费用上限约束；未核验正文不作为最终答案流式发布。发布前再次检查取消、删除、撤权及范围变更。

## 3. 数据身份、迁移与发布

### 3.1 三层身份与引用契约

| 层次 | 内容与版本变化 |
|---|---|
| 原文版本 | 复用 `material_version_id`，关联原始文件 hash；原文件内容变化生成新版本，旧文件不可原地覆盖 |
| 检索版本 | `retrieval_version_id` 绑定原文版本、解析产物 hash、parser/chunking/tokenizer/BM25/embedding/tree profile；处理方式变化生成新版本 |
| 学习范围快照 | `scope_snapshot_id` 绑定空间、资料版本、`graph_version`、允许原文跨度与可见性修订；绑定/图谱/范围变化生成新快照 |

原文定位采用 `SourceSpan(material_version_id, artifact_hash, start, end, page, block)`：文本区间为半开区间 `[start,end)`；`artifact_hash` 指不可变提取文本坐标系，不能直接跨解析器比较 offset。PDF 保存物理页/块及到原文件的定位，TXT/Markdown 保留原始字符映射；一个 chunk 可有多个跨度。转换产物到原始文件的映射必须验证，无法对齐时拒绝映射，不能靠相似文本猜授权。

新增引用采用 `citation_schema_version=2`，保存原文版本、检索版本、chunk ID、跨度和显示定位；旧引用无版本字段按旧 schema 读取。旧消息/图谱中的 chunk ID 不重写。旧引用可追溯不意味着已删除或无权访问的内容仍可显示。

范围映射顺序：旧学习图谱来源 → 对应旧解析产物原文跨度 → 核验后的共同来源坐标 → 新 chunk 全部跨度。新 chunk 的每个跨度都必须被允许区间并集完整覆盖；只重叠一部分则整块排除。首版不通过动态摘录挽救混有排除内容的块；记录整块排除数及由此产生的漏召回。完全位于允许范围内的块可为上下文预算选取准确定位的摘录，但必须保留结论依赖的必要条件与例外。

内容向量的新身份由原文版本、检索版本、chunk ID 与正文 hash 构成；`graph_version` 继续在学习范围中生效。旧身份适配器保留，不把删字段作为迁移。SQL 为正文权威；Qdrant/BM25 只返回身份和排名，公共层重新取正文并校验。

### 3.2 追加表与生命周期

追加 `rag_retrieval_versions`、`rag_chunks`、`rag_nodes`、`rag_chunk_spans`、`rag_scope_snapshots`、`rag_scope_spans`、`rag_scope_chunk_map`、`rag_manifests`、`rag_publications`。chunk/node 主键同时受检索版本约束；映射表唯一键为 scope + retrieval version + chunk。profile、来源映射和构建配置不可原地改写；发布指针可在事务中切换。

清单状态：`building → validating → ready`，错误为 `failed`，删除/撤权为不可发布状态。分别记录 `a_ready`、`b1_ready`；B1 必须额外通过结构关系校验，A 不等待树或摘要。每请求固定一个清单集合，记录各资料的检索版本，不能在重排阶段切换新索引。

迁移执行顺序：

1. 测试数据库备份、记录 Alembic head；追加表，保持旧读写路径可运行。
2. 按原文版本幂等回填原文跨度和范围映射；无法可靠对齐的学习空间继续旧路径，显式标为未迁移。
3. 在独立 collection/profile 构建 BM25 和向量索引；校验身份、hash、数量、维度、分词配置与树覆盖。仅“外部写入成功”不足以 ready。
4. SQL 事务 compare-and-swap 发布完整清单；并发发布竞争者失败并重读。外部模型/向量调用不持有数据库锁。
5. 中断重启按幂等构建键继续或清理未发布派生索引。构建失败保留仍有效的旧发布清单；索引回滚只切换经校验的指针，不回滚撤权/删除状态。
6. 数据/范围在回答途中变更时阻止过期结果发布。删除先写权威不可见状态，再幂等清理派生索引、树、缓存；清理未完成不能恢复可见。
7. 验证新旧并存后再考虑后续清理；本阶段不删除历史 chunk 或执行破坏性 downgrade。每请求发布检查同时约束旧适配器。

## 4. 按批次执行与验证

所有命令从实施工作区的 `py` 目录运行，使用 `uv run` 或同环境 Python；下文测试及 CLI 已有实现。测试先编写可观察业务断言、运行确认失败，再完成实现、重跑及审查。按独立任务提交，避免混入现有未提交资料。数据库迁移与真实服务测试仅使用专用测试环境。

### 批次①：数据契约、范围与迁移

**文件：** 第 1 节 `contracts.py`、`scope.py`、`rag_repository.py`、`db.py`、`0014_rag_snapshots.py`；修改 `source_access.py`、`knowledge/preparation.py`、`spaces/service.py`。新增测试 `py/knowpath_backend/test/test_rag_contracts.py`、`test_rag_scope.py`、`test_rag_migrations.py`。

- [x] 写入契约：`prepare(snapshot, profile, task_context) → manifest`；`retrieve(request) → candidates/status/error/trace`；`delete(identity) → receipt`；`close() → None`。请求固定 query/original_query、scope_snapshot_id、manifest_ids、预算、deadline；候选只带身份、通道、rank、score、parent_id。核验结果使用第 2.2 节枚举。
- [x] 建立测试：相同正文改图版本仍隔离范围；原文版本变化不能错配；新块跨允许/排除边界被整体拒绝；解析产物 hash 不同不能直接比较 offset；旧引用保持原始定位；删除后旧引用也不可读取；测验查阅新引用仍记 assistance。
- [x] 实现追加表与映射，运行旧消息/图谱范围测试；每次迁移检查 schema 与 ORM 一致。
- [x] 从第一批开始创建 `docs/research/tree-rag/evaluation/dataset.jsonl`、`rubric.md`、`split.json`；准备受控跨页、条件例外、范围排除、冲突、服务失败夹具，明确与真实资料分开。

```powershell
uv run alembic heads
uv run alembic upgrade head
uv run pytest knowpath_backend/test/test_rag_contracts.py knowpath_backend/test/test_rag_scope.py knowpath_backend/test/test_rag_migrations.py knowpath_backend/test/test_learning_graph_scope_contract.py -q
```

**验收：** 专用数据库升级成功，迁移 head 唯一；上述断言全部通过，无法映射的记录显式留在旧路径。新旧引用在允许情况下都可回查原文。

### 批次②：解析、语义分块与建库发布

**文件：** 新增 `parsing.py`、`chunking.py`、`lifecycle.py`、`cli.py`；修改 `materials/service.py`、`workers/material_ingest.py`、`rag/indexing.py`、`rag/retrieval.py`、`materials/deletion.py`、`spaces/deletion.py`。新增测试 `test_rag_chunking.py`、`test_rag_lifecycle.py`、`test_rag_services_integration.py`，均位于 `py/knowpath_backend/test/`。

- [x] 先写三种格式定位夹具和故障注入测试：跨页条文、短独立定义、公式/代码、重名小节、损坏内容；向量写到一半失败、重复构建、并发发布、删除期间重试。
- [x] 实现语义边界、多个来源跨度与输入上限检测。替换现有三处截断时逐一验证长文本进入分块/预算路径，不能直接把超长文本送入模型。
- [x] 核验真实 PDF 的 9 章、132 条和 37 页定位；检查教材公式、《论语》篇章。结构不确定与正文损坏分别标记。
- [x] 实现 build/validate/publish/rollback；CLI 接受 JSON 清单文件，失败返回非零退出码和稳定错误码。构建报告包含漏失文本、映射失败和 readiness，而非只有总 chunk 数。
- [x] 创建 `knowpath_backend/test/fixtures/rag/build.json`，只指向测试资料及测试存储。执行以下未来 CLI 流程，主动制造失败并验证旧发布仍可用。

```powershell
uv run pytest knowpath_backend/test/test_rag_chunking.py knowpath_backend/test/test_rag_lifecycle.py -q
uv run python -m knowpath_backend.learning.rag.cli build --config knowpath_backend/test/fixtures/rag/build.json --output .rag-test-manifest.json
uv run python -m knowpath_backend.learning.rag.cli validate --manifest .rag-test-manifest.json
uv run python -m knowpath_backend.learning.rag.cli publish --manifest .rag-test-manifest.json --expected-generation 0
uv run pytest knowpath_backend/test/test_rag_services_integration.py -q
```

**验收：** 无静默截断、跨页映射正确、失败不发布、不混用版本、恢复幂等。真实 Qdrant/数据库测试未执行时本批集成验收保持未完成。

### 批次③：普通 RAG 与公共回答链路

**文件：** 新增 `bm25.py`、`fusion.py`、`plugins.py`、`reranking.py`、`context.py`、`verification.py`、`pipeline.py`；修改第 1 节 conversations、config、provider 与 API 文件。新增测试 `test_rag_hybrid.py`、`test_rag_context.py`、`test_rag_verification.py`、`test_rag_pipeline.py`，位于 `py/knowpath_backend/test/`。

- [ ] 用开发集选择中文分词/BM25 实现、模型与 token 计数器，保存精确版本和 profile；共同标题前缀两组一致。未具备真正 BM25 前不得把单字匹配命名为 BM25。
- [x] 编写并实现通道去重和 RRF 测试：排名从 1 开始，单通道单 chunk 只贡献一次，采用 `sum(weight / (k + rank))`，不直接相加异构分数。记录重排前/后证据覆盖。
- [x] 实现组级预算裁剪；测试条件丢失时不发布完整结论、部分回答、多资料各侧引用、冲突并列、无命中不作全文否定、超时与取消。
- [x] 实现最多一次修正及再次核验；分别注入首次不支持、二次仍不支持、核验服务异常，验证状态和调用次数。未核验内容不能提前进入最终消息事件。
- [x] conversation service 只负责快照/任务/发布编排，把检索与核验逻辑留在 RAG 模块。保留活动测验确定性分支，并从普通问答实验中单列。
- [x] 扩展 `test_rag_services_integration.py` 真实调用 embedding、reranker、生成、核验，记录服务版本/响应和错误，不把替身当作真实集成。

```powershell
uv run pytest knowpath_backend/test/test_rag_hybrid.py knowpath_backend/test/test_rag_context.py knowpath_backend/test/test_rag_verification.py knowpath_backend/test/test_rag_pipeline.py knowpath_backend/test/test_learning_message_retrieval.py -q
uv run pytest knowpath_backend/test/test_rag_services_integration.py -q
```

**验收：** A 可独立回答；硬约束场景全通过，普通基线完成开发集真实调用，形成可复核错误记录。此时不能宣称真实质量已达到最终选择要求。

### 批次④：树形 B1

**文件：** 新增 `tree.py`、测试 `py/knowpath_backend/test/test_rag_tree.py`；修改 `plugins.py`、`lifecycle.py`，其余公共链路沿用批次③。

- [x] 先用带明确条件、续文及排除章节的结构夹具验证第 2.1 节算法，覆盖无父节点、重复种子、父节点热点、扩展不足、超过 40、跨版本父节点等情况。
- [x] 实现候选扩展与 trace；所有外部服务收到的内容必须已经通过 scope 校验。B1 readiness 不要求摘要，A readiness 不要求 B1。
- [x] 对 A/B1 检查同一叶子、同一基础召回、同一重排/上下文上限和公共生成配置；记录实际候选数、token、调用和额外读取量。
- [ ] 开发集逐题检查扩展是否找到必要条件、是否引入噪声；只在开发集调整数值。组装器不能偏向树插件，确定性约束两组使用同一套测试。

```powershell
uv run pytest knowpath_backend/test/test_rag_tree.py knowpath_backend/test/test_rag_scope.py knowpath_backend/test/test_rag_context.py knowpath_backend/test/test_rag_pipeline.py -q
```

**验收：** 有界扩展规则与范围/预算全部通过。树形候选存在本身不作为质量胜出的证据。

### 批次⑤：真实模型配对评测与方案选择

**文件：** 第 1 节 `rag_eval/` 五个文件；评测目录新增 `development.json`、`freeze.json`、`runs/`、`decision.md`；新增 `py/knowpath_backend/test/test_rag_eval.py`。

- [ ] 完成约 60 题人工核验标签，按问题家族划分约 40 开发/20 保留，同义改写不能跨组。每题包含 question_id、family_id、允许范围、文档快照、必要证据跨度、答案要点、可回答性、题型。
- [x] 为评分器写受控报告测试：部分回答不算全成功；服务失败计入全请求分母；配对失败不丢弃；金标不能进入在线 pipeline；不同解析/范围快照不能混为同一运行。
- [ ] 在开发集写定 rubric 和 `development.json` 配置，后者包含题集/split/rubric 路径、资料与范围快照、模型/提示词/profile、候选/token/调用预算、重复次数、统计方法和成本门槛。`freeze.json` 保存这些值及输入文件 hash。保留集运行前冻结，runner 拒绝缺字段及 hash 不匹配。
- [ ] 费用/延迟门槛由开发集容量与部署要求确定，填入数值字段 `max_p95_latency_ms`、`max_mean_cost_per_request`、`max_b1_latency_ratio`、`max_b1_cost_ratio`；未填数值不得启动保留集选择实验。未经实测不在文档中捏造用户预算。
- [x] 对同题运行 A/B1，交替顺序减少服务时段偏差；重复次数在冻结配置中写定，两组一致，不因某次结果不利而重跑挑选。保留原始失败与重试记录。dev-v3完整80次已归档，服务失败不删样；后续小样本预算诊断另行冻结，不替代原报告。
- [x] 形成开发集服务状态、错误码、候选/重排/上下文/引用跨度覆盖、延迟及逐题复核材料；附法规代理分析，所有人审质量与未知费用保持未知。
- [ ] 形成逐题配对胜负、错误归因、分题型结果、成本/延迟及不确定性报告。输出完整请求和服务成功子集两种统计，并明确分母。

以下为已实现 CLI 契约；`freeze` 从完成的开发配置生成不可变文件，`run` 必须验证冻结内容，`report` 不重新调用生成模型：

```powershell
uv run pytest knowpath_backend/test/test_rag_eval.py -q
uv run python -m knowpath_backend.rag_eval.cli freeze --config ../docs/research/tree-rag/evaluation/development.json --output ../docs/research/tree-rag/evaluation/freeze.json
uv run python -m knowpath_backend.rag_eval.cli run --freeze ../docs/research/tree-rag/evaluation/freeze.json --partition heldout --output ../docs/research/tree-rag/evaluation/runs/heldout.jsonl
uv run python -m knowpath_backend.rag_eval.cli report --freeze ../docs/research/tree-rag/evaluation/freeze.json --results ../docs/research/tree-rag/evaluation/runs/heldout.jsonl --reviews human-reviews.json --output ../docs/research/tree-rag/evaluation/decision.json
```

**验收：** 固定配置的真实 A/B1 报告可重放，关键语义判定经人工复核。若凭据/服务缺失，报告写明未运行部分和影响，本批不勾选完成。

## 5. 预先规定的验收与选择标准

### 5.1 硬约束先行

范围、版本、引用身份、候选/token/调用预算、删除/撤权/取消后的发布保护，在固定验收场景中要求全部通过。任一违规阻断该配置验收，不用平均正确率抵消；这不代表已经证明所有生产输入绝无违规。

至少覆盖：跨页条款与条件组、混合允许/排除块、不同图版本、旧引用、构建中断与并发发布、删除后回滚、生成期间变更范围、核验异常、二次核验失败、过期结果、活动测验。语义支持、推导有效性、全部要点覆盖另外人工核验，不能伪装成确定性保证。

### 5.2 主要指标与不确定性

主要指标为“符合题目要求且关键结论有证据支持的任务成功率”：可回答题要求必要要点完成且引用支持；部分回答单列；无答案题要求恰当说明证据不足且不编造。并列报告错答、错引、有答案误拒、无答案误答、服务失败。服务失败在全请求统计中为未成功，不能从分母删掉。

逐题错误归因为解析、召回、重排、上下文裁剪、生成、核验/引用、服务故障。报告候选和最终上下文的证据覆盖，以免把生成模型记忆误当检索收益。延迟记录 P50/P95、均次费用、实际模型调用和构建/更新成本。

统计方法作为开发集上的技术选择，在保留集运行前冻结；可评估按问题家族配对 bootstrap 等方法，记录所选置信水平、随机种子与重采样次数，不将某一种方法或区间下界大于 0 视为已经确认的采用硬门槛。重复运行先按题汇总，不能把重复调用当独立题。报告逐题胜/负/平和差异的不确定性，小保留集只支撑探索结论。

### 5.3 决策规则

| 结果 | 决策 |
|---|---|
| 任一硬约束失败 | 阻断该配置，修复后重新验收；不能以质量收益豁免 |
| B1 有可复核的质量收益，且费用/延迟满足预先确定的门槛 | 可考虑采用 B1；先记录受益题型与适用范围，并用未见资料验证泛化，不宣称普遍优胜 |
| 收益不明确、证据仅为多答对一两题，或成本超门槛 | 暂用通过验收的 A；补充未见资料。不得在看完保留集结果后放宽门槛 |
| 反复漏掉跨章节证据，且解析、重排、预算或生成问题已排除 | 才启动独立 B2 摘要导航实验，重新冻结实验方案 |
| 主要错误是胡编、条件裁剪或核验失败 | 优先修公共链路；不靠增加树层级宣称解决 |

保留集一旦用于调整配置即转为已见数据；后续确认使用新的保留集。若 A 自身未通过硬约束或回答边界验收，也不能因 B1 不胜出而自动上线 A。

## 6. 交付清单与后续执行边界

- [x] ① 数据契约、追加迁移、兼容引用与首批题集。
- [x] ② 可追溯分块、索引清单、故障恢复与真实存储集成记录。
- [x] ③ 独立普通基线、公共核验、真实服务集成与开发集报告。报告暴露配置及语义风险，勾选交付不代表质量验收通过。
- [x] ④ 有界 B1、完整 trace、共同预算与范围验收。
- [ ] ⑤ 冻结配置、真实保留集 A/B1 报告、逐题归因与采用结论。

实施顺序固定为共同基础与普通 RAG → 树形 B1 → 真实模型配对评测。每批分别保存确定性测试、真实服务集成、真实模型质量记录；前两类通过不替代第三类。工程实现与人工验收分开记录，不能把开发代码齐备勾选为全部方案验收完成。

## 2026-09-20 首批实施进度（历史记录）

已按修正后的计划开始实施，代码位于独立分支 `codex/rag-foundation`、项目内 `.worktrees/rag-foundation` 工作区。已实现数据/引用契约、原文范围映射、9 张追加表与迁移、不可变仓储、新旧引用和测验查阅审计桥接，以及删除事务兼容。最终相关回归 188 项通过、18 项跳过，包含 3 项真实 MySQL 临时库验证；10 道受控合成题的来源与划分校验通过。

这只是第一批基础实现。当前映射锚定旧解析 chunk，真实资料回填与新解析器映射、索引发布、普通 RAG 完整回答链路、B1 和真实模型对照仍待后续批次。现有业务库未迁移，在线检索未切换；上述开发复选框按完整批次验收保留未完成，不能将基础测试通过视为全部实施完成。

## 2026-09-21 续实施结果（当前状态）

最终学习/RAG回归：1265项通过、222项跳过、514项未选中；仅有一条既存依赖弃用警告。跳过不计为通过。

以上首批记录已由后续实现推进：普通A、树形B1、共同分块/范围/版本、发布恢复、删除、生成核验、
费用与调用预算、真实对话接入和冻结评测工具均已交付。代码在 `codex/rag-foundation` 分支的独立工作区。
最后补齐真实插件prepare/delete调用、统一可冻结的模型预算、费用估算、超时保护，以及真实对话的安全错误详情。

完整开发实验80次中30次正常返回、50次失败；独立24k/4k预算诊断10次中4次正常返回、6次失败。
后者没有记录到输入预算超限，仍有4次总期限超限、1次核验服务失败、1次生成结构校验失败。
原始失败、固定分母、源码及输入快照均保留。最后的对话错误发布修复发生在预算诊断归档之后，未参与该实验。
测试证据见[实施记录](../../research/tree-rag/IMPLEMENTATION-STATUS.md)，实际决策见
[当前采用决定](../../research/tree-rag/evaluation/decision.md)。

尚未完成：60题人工标签与回答支持关系复核、开发配置选择与新完整验证、实际价格及四个部署门槛、
保留集报告和最终采用决定。运行时默认继续为legacy，不以测试通过替代这些验收。
B2/B3与自动路由仍须满足原计划条件后再启动，不列为首版已实现能力。
