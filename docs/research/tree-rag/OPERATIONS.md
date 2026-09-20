# 普通 A / 树形 B1 操作说明

本实现保留旧检索入口，新增版本化的 A/B1。默认 `LEARNING_RAG_PLUGIN=legacy`；
设为 `a` 或 `b1` 才会进入新链路。两者需要 SQL 存储、已就绪且覆盖当前学习范围的发布清单。
业务库升级、默认方案选择与真实模型质量验收是不同事项。

## 构建与发布

在 `py` 目录、已安装项目依赖的环境执行。显式配置 `DATABASE_URL`、`QDRANT_URL`，
需要时设置 `QDRANT_API_KEY`。模型凭据沿用 `.env`，不要写入构建 JSON 或日志。
数据库迁移入口为 `alembic upgrade head`，新增迁移 `0014_rag_snapshots` 只追加表。
先在专用数据库演练和备份；本次实现未自动升级已有业务库。

1. 上传资料并创建学习空间，确定学习范围。构建配置使用实际的 `space_id`、
   `material_version_id`，参考 `py/knowpath_backend/test/fixtures/rag/build.json`。
2. `plugin` 为 `a` 或 `b1`；B1 复用 A 的原文叶子与向量，仅新增结构树。
3. 使用完整命令构建、读回校验并发布：

```powershell
python -m knowpath_backend.learning.rag.cli build --config build.json --output manifest.json
python -m knowpath_backend.learning.rag.cli validate --manifest manifest.json
python -m knowpath_backend.learning.rag.cli publish --manifest manifest.json --expected-generation 0
```

仅首次发布使用 generation 0；之后使用当前 SQL 发布代数。并发旧代数会失败，不能省略检查。
`rollback --manifest older.json --expected-generation N` 也是经过当前范围/原文/向量校验的发布，
不能恢复已删除或已撤权的内容。清单文件只提供身份，配置以 SQL 为准。
范围变更后必须为新范围构建/验证清单，旧范围不会隐式降级回答。

构建失败保留失败阶段与原发布；重试配置需显式 `retry:true`。重试读回已有向量，
只补缺失批次。build report 检查原文映射、非空白字符漏失/重复、损坏正文、允许和排除块数量。
解析坐标锚定不可变旧提取产物；不会声称修复旧解析器已经丢失的标题或版面信息。

## 运行预算与回答

两组共用混合召回、RRF、重排、上下文与生成核验；B1 的首轮配置为最多30种子、10扩展、
每父节点最多3扩展、总计40重排候选。参数是可复核的实验起点，未证明最优。
中文 BM25 使用 Okapi 公式及固定 Han unigram/bigram profile；不将它称为已选出的最佳中文分词器。
UTF-8 字节作为保守 token 上界，完整上下文按证据组选择，绝不裁切原文字符串凑预算。

模型请求预算通过 `RAG_MODEL_INPUT_TOKENS` / `RAG_MODEL_OUTPUT_TOKENS` 显式配置，
默认12000/2000；两者须为正整数，合计不超过 `LEARNING_CONTEXT_BUDGET_TOKENS`。
生成和核验使用同一配置，评测冻结文件也从同一入口读取；非法配置在外部调用之前拒绝。
默认数值在真实开发集中暴露预算失败，不能作为已验收配置直接部署；后续预算诊断及限制见运行报告。

`RAG_DEADLINE_SECONDS` 默认120秒，最大600秒；每次最多2次生成和2次核验，只有一次修正。
必要答案要点和每条结论分别记录核验结果；缺失内容转为部分回答或证据不足，服务异常明确失败。
外部模型只能提出语义判断，不能保证发现全部隐含条件、推导错误和提示注入。
期限到达后本地拒绝发布；已经提交的远端调用可能稍后完成或计费，不能保证远端取消。
传输层同时保留更短的供应商单次调用超时，不因总期限尚有余量而放宽它。
持久消息在模型调用前记录执行标记，崩溃/租约恢复后返回 `RAG_EXECUTION_INTERRUPTED`，不自动重跑。

金额控制通过 `RAG_MAX_REQUEST_COST` 与 `RAG_PRICING_FILE` 成对设置。
价格文件包含 `currency`、`date`、`price_table`；每个实际模型需声明
`input_per_million`、`output_per_million`，如有固定请求费再填 `per_call`。
必须依据实际供应商计费方式填入经确认的数值，不能复制测试中的虚构价格。
每次 provider 请求发送前，按完整输入字节上界、输出预留、重排重复查询和固定请求费保守预留。
共享预留超额则阻断下一次调用；失败不退还预留。该上界依赖所填价格/计费模型，不是供应商账单保证。
未配置时仅启用 token/调用/期限限制，trace 明确记录金额控制未配置，不能据此通过费用验收。
离线费用估算包括按token费用和每次HTTP请求的固定费用；embedding批次按实际调用计数。
聊天输出单价、使用量或必要的调用次数缺失时保持未知，不能把缺失项当零。

返回引用采用 `citation_schema_version:2`。新引用回查入口：
`POST /api/v1/learning-spaces/{space_id}/citations/resolve`，请求体为完整 Citation，
仍须遵守应用现有鉴权及 `Idempotency-Key` 中间件。旧引用保留原始身份和访问审计。

## 删除和不确定写入

资料删除先在 SQL 中不可见，清理任务精确删除捕获的 collection/material-version/retrieval-version。
每次外部写入前已有持久 `rag.index.write` 意图；存在未结束写入时不宣布清理成功。
运行时 BM25 缓存仅存活于当前请求，不在进程中跨请求保留已删除资料的词项缓存。

进程崩溃或写入网络结果不确定时，不能根据租约超时断言远端已经停止。操作员先确认原写入者及
已提交写入确实不会继续，再调用 `materials.rag_cleanup.reconcile_rag_write`，传真实 intent ID、
`writer_stopped=True` 及原因。该函数不是公开 HTTP 接口；随后持久删除任务重新扫除并验证。
没有这种确认时保持待清理状态。失败、恢复尝试及未知计费保留在审计记录中。

## 真实开发评测

`evaluation/real-draft` 提供60题草稿，40开发/20保留，问题家族不跨组；所有标签保持 `pending`。
真实原始样本来自项目 `docs/rag-sample-materials/chinese` 的公开资料；准备程序先核验 SHA256。
独立环境准备与运行：

```powershell
python -m knowpath_backend.rag_eval.bootstrap prepare --env-file PATH_TO_ENV --samples PATH_TO_PUBLIC_SAMPLES --dataset-dir ../docs/research/tree-rag/evaluation/real-draft --output .rag-evaluation/my-run
python -m knowpath_backend.rag_eval.bootstrap run-dev --env-file PATH_TO_ENV --output .rag-evaluation/my-run
```

prepare 中断可用同参数 `resume`，只适用于仍为 building 的原目录。
run-dev 冻结代码、配置、题集、来源和模型预算，交替执行40题 A/B1，共80个计划请求。
结果追加保存，无隐式重试；同一输出不允许覆盖或筛选重跑。冻结后不得修改依赖文件。
离线报告用 `rag_eval.cli report`，详细协议见 `evaluation/HARNESS.md`。

真实模型自己的 supported/answered 不是人工正确率。人工复核前，配对质量胜负、采用结论保持未知。
保留集须先完成标签/划分人工审核，冻结价格及四个延迟/费用门槛；程序拒绝缺少这些条件的运行。
B2/B3 摘要导航和自动路由属于原计划中的条件性后续实验，不因 B1 已实现而自动开启。

## 可靠性修复后的验证入口（2026-09-21）

从 `py/` 执行以下三个独立门禁，每次使用新的报告目录：

```powershell
python scripts/run_acceptance.py --profile offline --report-dir .rag-evaluation/offline-UNIQUE
python scripts/run_acceptance.py --profile store --report-dir .rag-evaluation/store-UNIQUE
python scripts/run_acceptance.py --profile model --live --model-env C:/PATH/TO/model.env --report-dir .rag-evaluation/model-UNIQUE
```

offline 使用明确的测试替身；store 自动创建独立 MySQL、Neo4j、Qdrant，验证迁移、服务版本、
事务和生命周期；model 同样使用临时存储，实际调用 embedding、rerank、生成及核验，
普通 A/B1 均经过消息发布和 HTTP 引用回查。模型用例的消息仓储为 fixture 独立 SQLite，
MySQL 事务与恢复另由 store 门禁验证。模型门禁只覆盖小样本链路，完整开发评测仍是单独步骤。

三个入口均保存预期/执行用例集合及结果；缺少必跑用例、意外 skip/xfail、零执行、迁移失败或
临时容器清理失败返回非零。39 个明确不适用的后端组合由矩阵排除；没有通过数冒充执行数。
脚本不读取业务数据库地址作为测试地址，也不迁移业务库。报告中的原始 pytest 日志仅保留本地，
分享时使用检查过的 gate/report 摘要，避免供应商或连接错误携带内部信息。

V2 使用请求内短来源 ID，草稿最多12条结论/12个要点，并按 `RAG_MAX_DRAFT_BYTES` 限制规范化 JSON。
代码将完整原文确定性划为带ID的片段，核验只选择 `evidence_id`，由代码恢复 Unicode 半开跨度。
全部片段保留原始字符，不能去空白、改写或截掉条件；未知ID或归属错误拒绝。
这避免要求模型计算字符偏移或逐字重抄PDF断行。最终对外引用身份保持 schema2。

生成器只看到 `sN + source_text`，核验器看到 `sN + segments(eN,text)`，避免混用来源与段ID。
strict schema按本轮来源、段、结论ID绑定枚举，规划与发送共享同一请求体；结论ID为c1…c12。
数学输出采用纯文本或Unicode提示，证据原文不改写。这不能保证供应商不再提前结束JSON；
即使finish_reason为stop，不完整JSON仍明确失败。版本标识纳入冻结配置。

修复验证配置采用 `RAG_RESPONSE_FORMAT=json_schema`、输入上限24000、输出4000、
模型上下文32768、草稿4000字节、总期限120秒；生成/核验成对准入的最低预留为10/25秒，
分别由 `RAG_REVISION_GENERATION_SECONDS`、`RAG_REVISION_VERIFICATION_SECONDS` 设置。
这些是实验配置与时间预留，不是供应商延迟保证，也未自动改写应用默认设置。

现阶段计数仍为带模板余量的 UTF-8 字节保守上界，不能称为真实 token 数。
trace 同时标注计数模式、计数器版本和供应商实际 usage；可选本地 tokenizer 必须以
`RAG_TOKEN_PROFILE_MANIFEST` 和 `RAG_TOKEN_PROFILE_SHA256` 绑定审核后的部署与文件，
不会自动下载或执行远端代码。参见 [计数来源](TOKEN-COUNTING-PROVENANCE.md)。

第二轮前检查完整证据组、两次模型输入/输出、调用数、时间与已配置金额；金额成对预约，
失败不退回。证据不足或需要澄清经首次核验确认后结束，不重复生成相同空答案。
已无足够资源时明确返回预算错误；核验失败不得转成资料不足或发布未核验正文。
请求 journal 与答案独立保留，失败仍记录已发生的物理调用、用量及阶段；未收到用量保持 unknown。
`content_passed` 只表示 JSON 内容检查通过，`passed` 表示本地结构/引用契约通过，不代表人工语义正确。

HTTP 边界使用固定上限32个工作线程，每个transport惰性占一个，队列有界。
调用方按绝对阶段期限等待响应头和响应字节；取消及close不会同步等待底层慢关闭。
原工作线程负责关闭迟到响应并释放配额，不能修改已封存journal。
Python线程无法强杀永久阻塞的底层调用；这类调用会占用固定配额，使后续请求到期失败，
不会无限创建线程。供应商远端完成、取消及最终计费仍不由本地截止机制保证。
