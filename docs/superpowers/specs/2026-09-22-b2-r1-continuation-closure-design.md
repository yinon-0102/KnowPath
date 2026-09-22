# B2-R1 Continuation Closure 检索设计

## 状态

设计已确认。本文只定义 B2-R1 的检索实验，不实现代码，也不改变生产默认模式。

## 背景与结论边界

当前 A/B1 真实运行已经证明服务链路可以稳定完成请求，但没有证明 B1 带来端到端答案质量提升。A 的 rerank Recall@10 为 95.42%，B1 为 96.67%；B1 的 375 个扩展节点只命中 2 个必要证据，扩展精度为 0.53%，NDCG@10 从 92.73% 降至 92.25%。B1 的实现本质是同父节点邻接扩展，不是有类型和来源证明的树遍历。

B2-R1 首先验证一个更窄、可证伪的假设：如果一个语义单元被解析器拆成多个 continuation 叶子，检索命中其中一个叶子时，完整恢复这个单元是否能提高必要证据召回，同时不污染单叶和邻接误导题。

B2-R1 不代表通用 tree RAG、知识图谱推理或多跳语义规划。父节点只用于导航和分组，最终进入 rerank、上下文和引用的对象必须是可追溯到原文的叶子。

## 目标

1. 在一次 query embedding、一次 rerank 和最多 40 个候选的约束下，验证 continuation 单元闭包的检索收益。
2. 让所有结构新增都能追溯到冻结 manifest 中的 continuation 关系、材料版本、检索版本和原文跨度。
3. 用单叶和邻接误导负控识别“结构闭包本身是否产生误扩展”。
4. 将候选召回、rerank、上下文装配和人工答案质量分开报告，避免把服务成功当作答案正确。

## 非目标

- 不实现自动关系抽取、模型推断的 requires/prerequisite/related-to 边。
- 不自动扩展普通同父兄弟，不实现种子之间的最短路径搜索。
- 不把父节点摘要当作证据，不允许父节点成为唯一引用来源。
- 不增加 embedding、BM25、rerank、生成或核验调用。
- 不改变 SQL 授权、原文重建、引用 schema、生成器或核验器。
- 不在 heldout 通过前启用生产默认流量。

## 术语和结构契约

### 证据单元

对已授权叶子行使用以下单元键：

```text
unit_id = continuation_of or chunk_id
```

一个单元的所有成员必须满足：

- `material_version_id` 相同；
- `retrieval_version_id` 相同；
- `parent_id` 相同；
- 原文 source span 均属于当前 scope；
- `ordinal` 是非负整数且能确定稳定顺序；
- 成员的结构信息来自冻结 manifest/tree version，而非模型响应。

如果任一条件不满足，结构闭包不可用，插件降级为 A 的候选结果。

### 结构来源

B2-R1 只接受解析器能够确定的结构：父节点关系、同级顺序和 continuation。父节点不进入证据候选。普通同父兄弟关系仅作为元数据和顺序信息存在，不触发自动扩展。

## 检索架构

```text
问题
  -> A: BM25 + 向量 + RRF，最多 40 个 seed
  -> ContinuationClosure
       -> 校验授权叶子和冻结结构
       -> 对 A seed 触发完整 continuation 单元闭包
       -> 闭包放不下时保留原 seed，不做部分扩展
       -> 最多 40 个叶子候选
  -> 既有一次 rerank
  -> 既有 context assembler、生成和核验
```

### 闭包算法

1. 保留 A 的有序候选列表及其 rank，作为不可变基线。
2. 按 A 候选顺序第一次遇到一个 `unit_id` 时，取该单元全部授权叶子并按 `ordinal` 排序。
3. 如果完整单元能够放入当前候选容量，并且其源文本估算能够放入当前上下文预算，则输出整个单元。
4. 如果完整单元放不进候选或上下文预算，则不扩展该单元，只输出原 seed。
5. 后续遇到同一单元的 A seed 时去重。
6. 处理完结构闭包后，用尚未输出的 A 候选按原 rank 填充空位，直到达到 40 或没有候选。
7. 所有输出均为叶子身份；输出顺序和替换关系写入 trace。

该算法没有连续权重或人工加权分数。结构扩展只有“完整闭包可容纳”与“保留原 seed”两个结果。候选容量和上下文容量同时约束，避免候选 Recall 上升后在上下文装配阶段再次丢失证据。

### 基线保留和替换

每条 B2-R1 结果必须保存 A 的原始候选列表，并报告：

- `a_retention_at_40`：B2 保留的 A 候选比例；
- `structural_additions`：新增叶子数量；
- `replacements`：被结构闭包挤出的 A 候选及原因；
- `skipped_closures`：因候选或上下文容量未扩展的单元。

不允许通过隐式 anchor、固定扩展槽位或未记录的排序规则静默改变 A 的结果。

## Trace 和运行安全

每个结构新增记录 `unit_id`、seed chunk、目标 chunk、`edge_type=continuation`、`tree_version_id`、材料版本、检索版本和跳过/替换原因。ManagedPlugin 仍只发布候选身份，不发布插件生成的证据文本。

结构元数据缺失、版本不一致、跨材料引用或 source span 越权时，B2-R1 返回 A 的候选并记录 `structure_unavailable`。A 的 embedding、Qdrant、rerank、deadline 或授权失败仍按原错误失败，不伪装成结构降级。B2-R1 不增加任何 provider 调用，也不增加 harness retry。

## 评测数据集

目标题集为 60 题：40 道 development，20 道 heldout。若三份真实资料无法提供足够的合格题目，则减少题数并停止，不用机械相邻题凑数。每个分区保持：

- 50% continuation 必要题：必须覆盖同一 continuation 单元中的至少两个叶子；
- 25% 单叶负控：一个叶子足够，邻接叶子明确不必要；
- 25% 邻接误导负控：相邻或同父叶子语义相近，但不能作为必要证据。

每题必须绑定精确 `necessary_evidence` 原文跨度、`structural_required`、`continuation_unit_id`、负控的 `forbidden_structural_additions`、材料哈希、版本和人工审核状态 `approved`。当前 `pending` 题库只能用于开发，不可作为采用依据。正例必须经过人工确认“缺少第二叶子就无法完整回答”，不能仅因 ordinal 相邻而入集。

## 评测指标

A 与 B2 使用同一冻结模型、Embedding、Qdrant、数据库、问题顺序和 reranker，分别报告候选列表和 rerank 列表：

- Recall@1/3/5/10/20/40；
- 固定 gold 理想值的 NDCG@10；
- MRR；
- continuation unit 完整召回率；
- B2 新增叶子的必要证据命中率；
- A 保留率、结构新增数、替换数和闭包跳过数；
- 单叶负控结构扩展率；
- 邻接误导负控误扩展率；
- 上下文证据覆盖率和引用证据覆盖率；
- 服务成功率、P50/P95 延迟和调用计数。

NDCG 的理想相关项数量必须来自题目的固定 gold 证据单元，而不能使用“本次列表里命中了多少相关项”作为理想值。候选、rerank、上下文和引用是不同阶段，不能合并为一个分数。开发集和 heldout 都对题目级 delta 做 bootstrap 置信区间；报告只作描述性比较，不把未评审答案宣称为正确。

## 采用门槛

B2-R1 只有同时满足以下条件才进入下一阶段：

1. continuation 必要题 Recall@10 比 A 至少提高 3 个百分点；
2. 全部题 NDCG@10 相对 A 的下降不超过 1 个百分点；
3. 单叶负控结构扩展率为 0；
4. 邻接误导负控误扩展率为 0；
5. P95 延迟不超过 A 的 1.2 倍；
6. 服务失败数为 0；
7. development 和 heldout 的提升方向一致，并且没有只在开发集出现的单题异常收益。

正例召回上升但 NDCG 或负控恶化时拒绝；正例没有稳定提升时停止 R1，不继续调权重；外部服务失败单独归类，不能混入检索质量结论。

## 实现边界

新增 `learning/rag/continuation.py` 作为只负责结构闭包的模块，并在 registry 中登记 evaluation-only 的 `b2_r1`。评测层新增题集冻结、固定 gold NDCG、unit recall、A retention 和误扩展统计。现有 OrdinaryPlugin、原文重建、授权、reranker、context assembler、生成和核验保持不变。

测试至少覆盖：

- 单叶、双叶、多叶单元；
- 重复 seed 和稳定 ordinal 顺序；
- 跨材料/跨版本/越权结构拒绝；
- 闭包无法容纳时保持 A；
- 40 候选上限和上下文预算上限；
- 闭包幂等、候选不重复、只输出授权叶子；
- embedding 和 rerank 调用次数与 A 相同；
- 评测器固定 gold NDCG、unit recall、A retention 和负控统计。

## 发布顺序

1. 审核并冻结 development/heldout 题集和结构 manifest。
2. 先运行离线协议测试，确认没有 synthetic gold 冒充真实模型结果。
3. 在隔离配置下运行 development A/B2-R1，分析逐题和分域结果。
4. 只有 development 满足门槛才运行 heldout；heldout 不得用于调规则。
5. 两个分区都通过后，才另行讨论生产开关、回滚和成本预算。

在上述流程完成前，B2-R1 不进入默认插件、默认 manifest 或生产流量。
