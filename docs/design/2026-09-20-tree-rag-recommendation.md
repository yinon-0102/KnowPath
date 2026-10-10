# KnowPath 树形 RAG：原始资料调研后的推荐

日期：2026-09-20。状态：研究与设计，未修改 RAG 实现，未恢复路由。

## 结论

推荐首版采用「文档结构树 + 共享原文混合召回 + 有预算的父子上下文检索」，之后单独增加「章节摘要跨层召回」。不从一开始递归生成所有节点摘要，不把逐层从根下钻作为唯一入口，不把未经本项目评测的任何方法称为通用最优。
上一版方案把摘要生成列为树插件默认前提；本稿修正为可选增强，以分离结构本身与模型摘要各自的收益。

## 原始资料与证据边界

1. RAPTOR 原论文：https://arxiv.org/html/2401.18059v1
   - 方法对原文 chunk 递归 embedding、软聚类、摘要；软聚类允许一个节点归属多个簇，因此不是严格的文档目录树。
   - Querying 小节比较 tree traversal 与 collapsed tree，跨层检索在其 QASPER 子集的对比中更好，主实验使用 collapsed tree。
   - 这是指定模型、数据集和预算下的实验证据，不是对中文学习资料的性能保证。
   - 论文对摘要的检查发现约 4% 包含轻微幻觉；作者表示在该实验中没有观测到对问答的明显影响。不能因此免除本项目的原文证据校验。
2. LangChain ParentDocumentRetriever 官方参考：
   https://reference.langchain.com/python/langchain-classic/retrievers/parent_document_retriever/ParentDocumentRetriever
   - 核心机制是检索小块，再通过 parent ID 获取更大的父文档，兼顾检索粒度和上下文。
   - 这说明树形检索不要求预先生成 LLM 摘要。
3. LlamaIndex AutoMergingRetriever 官方实现：
   https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/retrievers/auto_merging_retriever.py
   - 先做向量片段检索，再根据命中的子节点占比合并到父节点；默认 simple_ratio_thresh=0.5。
   - 固定比例是框架默认值，不是本项目最佳值。大父节点、范围排除和 token 预算都必须另行约束。
4. Docling 官方：
   https://github.com/docling-project/docling
   https://docling-project.github.io/docling/concepts/chunking/
   - 提供阅读顺序、版面、表格和 OCR 等文档解析能力。
   - HybridChunker 基于结构层级做 token 长度控制，必要时拆分、按相同标题合并相邻小块。
   - 可作为本项目统一解析格式的首个候选；未在本机安装或对中文样本实测，不能断言其中文解析一定优于其他工具。
5. Anthropic Contextual Retrieval：
   https://www.anthropic.com/engineering/contextual-retrieval
   - 为 chunk 添加上下文再做 embedding/BM25，并与 rerank 联合评测。
   - 文中 49%/67% 是其评测条件下 retrieval failure rate 的相对降低，不是本项目的回答准确率提升。
   - 本项目初期只使用确定性的文档标题/章节路径作为检索前缀，不把这种简单前缀直接等同其 LLM 生成 contextual retrieval。
6. PageIndex 官方实现与文档由独立调研补充：
   https://github.com/VectifyAI/PageIndex
   https://docs.pageindex.ai/sdk/agents
   - 文档树与基于模型推理的检索路线值得作为额外对照，不据项目自报榜单直接替代现有检索。
   - Agent 集成的 document_context 提示不限制工具访问；但内置本地 chat(doc_id=...) 提供结构化白名单，不能笼统说整个 SDK 无范围限制。KnowPath 的用户权限和资料版本仍由自己的服务层约束。
   - 官方 OSS 基准为 62 题/34 份 PDF，限定正文事实查找，明确排除表格、图片、算术，并排除拒绝索引文档：https://github.com/VectifyAI/PageIndex-OSS-Benchmark 。不能外推到本项目所有资料。
   - 宣传的 98.7% 对应 Mafin2.5 在 FinanceBench public set 的完整系统表现，不是目录树自身准确率：https://github.com/VectifyAI/Mafin2.5-FinanceBench 。
   - 当前 local_api 仅支持 PDF，本地 Flash 不执行 OCR；Markdown 另有独立建树脚本。引用：https://github.com/VectifyAI/PageIndex/blob/main/pageindex/local_api.py ，https://pageindex.ai/blog/pageindex-flash 。

## 当前样本的实际检查

food-safety-guide-zh.pdf：44 页，pypdf outline 顶层数量为 0。印刷目录存在，但 pypdf 提取顺序为第一章、第三章、第六章、第五章、第二章、第四章，标题和页码分列。这说明「有文本层」不足以保证「有正确目录树」。

因此第一步是结构解析验证，而不是立即树检索：
- Markdown：按标题语法树解析；保留数学、代码、列表边界。
- TXT：使用显式篇章标题。论语应形成书→篇→章句，不能仅按空行堆叠。
- PDF：综合印刷目录、阅读顺序、标题块和正文锚点，区分物理页码与印刷页码。模型只可提议结构，最终节点必须有实际原文锚点。
- 树节点记录结构来源和置信度；无法可靠识别则标记页组/窗口，不能冒充章节。源文档页面在局部核验中仍是依据。

## 推荐分层实现

### 共同底座

同一文档版本、同一规范化正文、同一叶子 chunk、同一 embedding、关键词分词与原文定位服务于两插件。
每个 chunk 分别保存 source_text 与 retrieval_text：后者可为标题路径 + 原文，前者只保留可引用正文。两套插件同样使用该前缀。
初始叶子目标 300～600 tokens；父级上下文约 1200～1800 tokens，仅作实验起点。自然节过长时加入 context_window 中间节点，不伪造新的章节名。表格保留表头与行关系，代码块不随意截断。
修复目前检索/indexing 两处 text[:6000]，确保完整叶子内容可检索；旧 chunk/index profile 作为历史版本保留。

### B1：结构树 + 父子上下文，无模型摘要

1. 使用与普通插件相同的 BM25 + 原文向量召回与内部 RRF，形成种子原文候选。
2. 依据真实 parent ID 汇总命中，同节命中集中时优先补充该父级中相关的相邻原文；记录 parent coverage 与 provenance。
3. 限制父级窗口、每个父级候选数、总候选数；不能自动加载整本资料，也不能仅凭 0.5 默认阈值无限合并。
4. 输出原文叶子候选及父级分组信息。公共层统一 rerank，按相关性、覆盖和上下文预算选择；所有实际提供给模型的文本都有原文 ID。
5. 普通插件不扩展父节点，两插件采用相同最终 token 预算。B1 的收益应明确归因于层级上下文选择，而非新的语义信息。

B1 解决局部上下文断裂，不保证整篇概览更好，这是下一实验项的目标。

### B2：在同一树上增加章节摘要跨层召回

按需要为章节/小节生成短摘要，并保持来源叶子 ID 和模型/提示词版本。摘要是派生索引，不是新的事实源。
直接同时查询各层摘要，命中摘要后在允许后代中按问题召回叶子；同时保留原文直达通道。
每个通道最终转换为相同 leaf_chunk_id 的排名，树内合并重复父节点贡献，避免同一原文沿多个祖先重复投票。
首个版本只把摘要用于定位，最终答案仍基于原文。将摘要加入生成上下文应列为 B3 单独实验。
该方案明确是“结构树 + 原文直达”的组合式树插件，不标榜为纯逐层树遍历；记录额外查询成本。

### 树节点与原文边界

SQL 存 node_id、parent_id、node_type、完整路径、document_version、source_spans/leaf_ids、structure_quality、tree_profile；摘要字段增加 summary_profile 和证据映射。
Qdrant 管理原文/摘要向量集合或命名配置。复用当前存储即可，不必新增图数据库；已有知识图谱继续表达前置与相关关系。
树节点身份使用文档版本、完整路径和原文位置，不以标题文本做唯一 ID。
摘要来源全部满足当前 allowed chunk 集合时才允许参与查询；部分不可见的父级不能把其整段摘要交给模型或用于外部重排。
资料删除、新版本发布、索引更新复用现有任务与可见性机制，分别记录 ordinary/tree readiness。

## 评测：从最小改动分离收益

| 组 | 变化 | 检验目标 |
|---|---|---|
| A | 同一叶子混合召回 + RRF + 公共 rerank | 合理普通基线 |
| B1 | A + 有界父级原文上下文 | 文档结构的独立价值 |
| B2 | B1 + 摘要跨层召回 | 摘要导航对概览/综合的增益 |
| B3（可选） | B2 + 可追溯摘要进入生成 | 摘要作为生成上下文的净收益 |
| C（后续） | RAPTOR 语义摘要树或 PageIndex | 只有前几组揭示瓶颈时再测 |

第一轮约 60 道人工证据核验题，按事实、解释、概览、比较、追问、不可回答/范围排除分组。调参集与保留测试集按问题家族拆分。
固定模型、共同分块、最终上下文 token 预算和公共 rerank；报告候选和调用预算，不把更多调用或更长上下文的收益误归于树。
重点记录证据 Recall@K、预算裁剪后覆盖、概览要点覆盖、正确性、引用支持率、拒答/误拒、P50/P95、在线与离线成本。增加小规模证据受控题，防止公开资料被模型凭记忆答对。
B1 未改善的问题类型无需强制启用它；B2 只有稳定改善概览/综合且成本可接受时才启用。自动路由在这些结果之后设计。

## 最小可执行研究步骤

1. 对 44 页 PDF 抽取目录页及各章首尾定位，验证六章边界和物理/印刷页码映射；核对 MD/TXT 的标题层级。
2. 选择 Docling 作为解析适配器候选，与现有 pypdf 在上述固定页面对比；必要时对照 MinerU，按实际正确率/资源成本选，不按品牌定结论。
3. 固定统一叶子版本，完成 A/B1 的相同预算比较。
4. 仅当概览/综合仍明显漏证据时，构建 B2 所需摘要节点。

最终推荐：先研究和验证“可追溯的文档结构”，首个树插件做父子原文检索，再按实验需要叠加摘要跨层召回。
