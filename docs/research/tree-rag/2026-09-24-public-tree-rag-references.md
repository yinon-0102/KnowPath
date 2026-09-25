# 可借鉴的树形 RAG 实现与当前 B3 的边界

核查日期：2026-09-24。来源为论文原文和官方项目说明；未安装依赖、未运行第三方项目、未向它们上传资料。当前 B3 未证明稳定净收益，不等于所有树形 RAG 都无效。

## 1. RAPTOR：递归聚类与摘要树

- 作者实现：https://github.com/parthsarthi03/raptor
- 论文：https://arxiv.org/abs/2401.18059
- 此次核查正文版本：https://arxiv.org/html/2401.18059v1
- 流程：原文分块 → embedding/聚类 → 为簇生成摘要 → 递归形成更高层摘要。查询可同时检索原文与摘要层。
- 树来自语义聚类，不等同于原文目录树。摘要节点提供跨片段的信息概括，主要用途是跨段综合、章节主题、长文本问答。
- 论文表 3：同用 GPT-4，QASPER Answer F1：DPR 53.0，RAPTOR 55.7，增加 2.7 分。表 4：同用 GPT-3，QuALITY dev accuracy：DPR 60.4%，RAPTOR 62.4%，增加 2 个百分点。这些是作者实验，不能预测 KnowPath 的收益。
- 论文比较了逐层遍历与 collapsed-tree（将各层节点统一检索）。在用于选择检索方式的 QASPER 子集上后者更好，主要实验采用后者。因此收益不能简单归因于“沿树从根走到叶”。
- 摘要中提到约 20 个百分点的 QuALITY SOTA 提升，比较的是完整系统，含不同回答模型；不能把它当作同一模型下单独增加树的收益。
- 仓库有加文档、问答、树的保存/加载及自定义模型示例。它是研究参考实现，迁入当前系统仍需适配。
- 值得借鉴：在索引中增加可检索的跨段概括，并保留摘要到原文叶子的可追踪关系。若用于严格引用，应回到原文校验；这属于面向本项目的改造建议，而非作者实验已验证的相同方案。

## 2. LlamaIndex AutoMergingRetriever：检索叶子，按需合并父块

- 官方示例：https://developers.llamaindex.ai/python/examples/retrievers/auto_merging_retriever/
- 开源框架：https://github.com/run-llama/llama_index
- 示例用 HierarchicalNodeParser 建立粗到细的块层级，默认 2048/512/128。叶子入向量检索，其余层级保存在 docstore。
- 命中同一父节点的子块达到阈值后，递归合并为较大父块。用于补全定义、条件、解释等上下文，减少小块割裂。
- 它不一定需要模型推理来导航，接入现有检索链的改动相对小；但父块会占用更多上下文，阈值也是参数。
- 示例含基线对照及 correctness/semantic similarity/relevance/faithfulness 评价代码，不能当作适用于所有任务的独立效果证明。
- 与当前方案不同：B2-R1 侧重 continuation 闭包，父节点不直接作为最终上下文；AutoMergingRetriever 会把更完整父块作为上下文。不能把两者称为同一种方案，也不能据此预期必然明显超过 B2。

## 3. PageIndex：目录树与模型推理导航

- 项目：https://github.com/VectifyAI/PageIndex
- 文档：https://docs.pageindex.ai
- 当前 README：https://raw.githubusercontent.com/VectifyAI/PageIndex/main/README.md
- 流程：为文档生成层级索引 → 模型根据问题在树中选择节点 → 读取相关原文 → 回答。项目主张使用推理导航而非向量相似性作为主要定位方式。
- 当前 README 描述 SDK local mode，可在本地运行索引和检索流程并使用自己的模型 Key；“本地模式”不表示所选模型一定在本地推理。
- 当前 Flash 说明：从文档布局提取树，模型用于摘要与细化；chat 模型负责搜索。开源本地模式主要面向文字型 PDF；Cloud 的解析/OCR/文件级树等能力不能混同为所有本地功能。
- 适合参考的场景：长法律文书、教材、技术手册和财报，需要先定位章节再读原文的问答。是否比当前混合检索更好仍需同条件测评。
- 项目 README 有作者自报的 FinanceBench 98.7% 指标与公开结果链接：https://github.com/VectifyAI/Mafin2.5-FinanceBench 。不可把系统自报成绩或图中的 vector-RAG 基线直接用于本项目结论。
- 当前 README 还链接本地开源流程测评：https://github.com/VectifyAI/PageIndex-OSS-Benchmark ，描述为来自 MMLongBench-Doc-V2 的 34 个 PDF/62 道文字查找题。此次只核查到 README 的声明，未复现该测评。
- 代价：多步模型调用、模型能力依赖、导航漏选风险；不能假设满足当前一次 embedding/一次 rerank/P95 ≤ A×1.2 的约束。

## 对 KnowPath 的判断

当前 B3 主要验证语义单元召回、叶子展开与受限上下文装配；此前主动限制了模型推断结构和父节点充当最终证据。这是合理的工程实验边界，但覆盖不了 RAPTOR 的摘要索引收益，也覆盖不了 PageIndex 的模型导航收益。

借鉴优先级应按目标区分：

1. 看“完整树导航系统如何工作”：优先 PageIndex，法律/教材的文档形态与我们接近，成本约束不同。
2. 最小范围接入现有向量/混合检索：优先 LlamaIndex AutoMergingRetriever，重点观察父块完整性和 token 开销。
3. 验证跨章节概括、主题综合：优先 RAPTOR，关键变量是多层摘要表示，而不是继续调整叶子排序。

普通 RAG 继续作为默认方案，不等于删除树结构。先处理已确认的共享核验误判，再比较不同检索架构，才能减少生成/核验故障对检索结论的混淆。不为新方案刻意重写有利题目；如业务确实需要新的任务类型，应单独标记并提前冻结评价口径。

公开来源本地阅读副本位于 `py/.rag-evaluation/tree-rag-project-research-20260924/`。未执行第三方代码、未修改运行策略、未提交或推送。
