# 预算诊断：预先指定的小样本

在完整 dev-v3 结束前写定本诊断：仅运行 `real-law-03`、`real-law-13`、`real-math-01`、
`real-math-04`、`real-lunyu-02`，每题 A/B1 各一次，交替执行，共10次请求，不自动重试。
这些是已见开发集失败题，专门用于发现配置问题，不能作为随机样本或总体质量提升证据。

两组统一配置 `RAG_MODEL_INPUT_TOKENS=24000`、`RAG_MODEL_OUTPUT_TOKENS=4000`、
`LEARNING_CONTEXT_BUDGET_TOKENS=32768`。完整输入仍按UTF-8字节保守计数；输出预留4000 token。
候选、证据上下文、生成/核验调用数、120秒总期限保持原配置，不截断证据或跳过核验。
不修改供应商模型、原文资料、学习范围、原始80次结果或人工标签。

本轮代码同时包含生命周期接入、费用估算和更严格的单次调用超时保护；因此报告版本差异，
不将前后所有差异单独归因为预算。价格为空时费用仍未知。

目标是判断现有协议的输入/输出空间是否导致失败，记录扩大预算后的实际状态、耗时和安全用量。
成功返回不代表内容通过人工复核；失败仍完整保留。无论结果如何，不运行保留集、不宣布选用B1。

当前状态：10次请求已完整执行，A/B1各2次正常返回、各3次失败；没有删除失败或追加选择性重跑。

## 结果

| 题目 | 组别 | 原dev-v3状态 | 本次状态 | 本次秒数 |
|---|---|---|---|---:|
| real-law-03 | A | MODEL_INVALID_RESPONSE | RAG_DEADLINE_EXCEEDED | 120.13 |
| real-law-03 | B1 | MODEL_INVALID_RESPONSE | MODEL_UNAVAILABLE | 116.05 |
| real-law-13 | B1 | MODEL_TOKEN_BUDGET_EXCEEDED | GENERATION_INVALID_RESPONSE | 45.76 |
| real-law-13 | A | MODEL_TOKEN_BUDGET_EXCEEDED | insufficient | 54.79 |
| real-math-01 | A | MODEL_INVALID_RESPONSE | RAG_DEADLINE_EXCEEDED | 120.09 |
| real-math-01 | B1 | MODEL_INVALID_RESPONSE | answered | 59.34 |
| real-math-04 | B1 | MODEL_TOKEN_BUDGET_EXCEEDED | RAG_DEADLINE_EXCEEDED | 120.06 |
| real-math-04 | A | MODEL_TOKEN_BUDGET_EXCEEDED | RAG_DEADLINE_EXCEEDED | 120.06 |
| real-lunyu-02 | A | MODEL_TOKEN_BUDGET_EXCEEDED | answered | 27.54 |
| real-lunyu-02 | B1 | MODEL_TOKEN_BUDGET_EXCEEDED | answered | 28.19 |

本轮没有记录到输入预算超限，但有4次120秒总期限超限、1次首次核验服务不可用、1次第二次生成的结构校验失败。4次正常返回包括3次answered、1次insufficient。这些状态均未经过人工语义评分；不得将4/10改写成回答正确率或总体提升。

原10个已见失败样本中有4个在本轮正常返回，仍不能单独归因预算：本轮另有代码修复，模型调用本身也有波动。两组各一次、定向选择失败题不支撑方案优胜或稳定性能推断。

较大预算配置仍未通过验收。不能仅继续提高上限就宣称解决；后续需要结合人工核对后的必要要点、结构化生成/核验协议的服务可靠性和可接受延迟/费用来选择开发配置。保留集仍未运行，所有人工标签、价格和门槛保持待定。

完整机器统计见 [summary.md](summary.md)，安全错误阶段及原文/回答见 [review.md](review.md)。
