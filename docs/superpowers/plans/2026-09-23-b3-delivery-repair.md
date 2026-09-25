# B3 证据装配与答案交付修复计划

> **For agentic workers:** 实现时使用 subagent-driven-development 或 executing-plans，按下列任务逐项执行并审查；本文件已于 2026-09-23 获准执行；运行状态见执行记录。

**Goal:** 修复已复现的证据组边界错位和可信部分答案丢失，明确降级口径，再公平比较修复后的 A、B2-R1、B3。

**Architecture:** 三种模式继续共用现有上下文与核验路径。装配补救只调整既有候选的顺序；答案恢复只使用本请求中通过全部检查的旧快照。检索策略、模型、向量表示、金标准保持不变，新增指标作为独立版本的分析结果。

**Tech Stack:** Python 3.13、pytest、Pydantic、现有 RAG pipeline、SQLite、Qdrant、DashScope。

**状态：** 2026-09-23 已实现并通过 800 项回归与独立审查，120 条离线重放无回退；360 请求真实测评运行中，尚无最终在线结果。未提交、未推送。继续遵守“先分析结果，再由用户决定是否提交”；获准提交时使用 `fix:中文说明` / `test:中文说明` / `docs:中文说明`。

---

## 1. 依据、目标与取舍

工作目录为 `C:/Users/27202/Desktop/KnowPath/.worktrees/rag-foundation`，以下项目内相对路径均以此为根。当前分支 `codex/rag-foundation`，已有未提交改动必须保留。

依据：`docs/research/tree-rag/2026-09-23-b3-citation-gap-audit.md`，以及主工作区 `.tmp/b3_followup_audit.py` / `.json`。

已验证：

- 120/120 条上下文 ID、顺序、原文与容量 trace 可由现有代码离线复现。
- 第 09 题 B3 的必要条文被一个完整定义组挤掉。以整组边界插入的离线探针可在原 14,000 输入预算内恢复覆盖；尚未正式实现。
- 终局契约错误会清空先前通过校验的部分答案。A 第 07、20 题有对应真实轨迹，但须进一步验证正式恢复条件，不能承诺一定恢复所有旧结论。
- 32/40 题 A/B3 首次生成请求体完全相同，对总引用覆盖差异贡献 +8.75 个百分点；全体差异为 +9.1667 个百分点。不能把该差异直接归因于树形检索。

选择：先做局部确定性修复，再验证同配置下的策略收益。直接提高预算或放宽校验不能解决上述程序行为；本轮也不重做 B3、引入加权调参、改成其它树结构，或更换题目寻找正结果。

验收分两层：代码修复不以 B3 获胜为条件；B3 的采纳继续以预先声明的指标门槛判断。修好共享路径可能同时改善 A。

## 2. 固定边界

| 项目 | 约定 |
|---|---|
| 数据 | 原 40 题、67 段必要证据、三份原文、原 split/rubric 不变 |
| 索引 | 970 叶子、934 单元、原叶向量及单元 centroid 表示不变；不重建索引 |
| 模型 | 沿用当前冻结的模型、端点、JSON 协议和提示词；不切换结构化输出模式 |
| 检索 | 每请求最多 40 个叶子候选、一次 query embedding、一次 rerank |
| 容量 | 输入 14,000、上下文窗口 16,000、输出 2,000、草稿 4,000 字节、核验预留 8,000 和证据预算 5,000 均不变 |
| 调用 | 最多两次生成、两次核验；传输和评测器重试均为 0 |
| 新在线主实验 | 建议统一 240 秒总时限、120 秒单次模型超时，仅用于此次独立实验；线上默认 120/60 不改 |
| 外发范围 | 后续获准执行时只使用既有三份资料和 40 题，仍发往已授权的 DashScope；不增加资料或服务商 |
| 原记录 | 原始运行、失败、重跑、派生 freeze、历史报告均不覆盖 |

采用 240/120 是为了减少已知 124 秒级请求被截断，让本轮比较聚焦交付质量；全部模式使用相同上限。它是新的实验条件，不能据此声称满足旧 120/60 的时延表现，不能用新旧分数直接量化代码修复收益。

## 3. 文件职责

| 文件 | 计划变更 |
|---|---|
| `py/knowpath_backend/learning/rag/context.py` | 求最大依赖闭合基线前缀；在完整保留段边界插入有效缺失锚点 |
| `py/knowpath_backend/learning/rag/pipeline.py` | 补救结果采纳保护、固定枚举诊断；保持最终 scope/cancel/deadline 检查 |
| `py/knowpath_backend/learning/rag/verification.py` | 可信快照、受限恢复、清单原子提交、准确降级文案及原因；限制错误类型 |
| `py/knowpath_backend/learning/rag/schema_diagnostics.py` | 引用契约细分类的固定枚举 |
| `py/knowpath_backend/learning/rag/diagnostics.py` | 新诊断字段白名单过滤，保留失败事实 |
| `py/knowpath_backend/rag_eval/delivery_analysis.py`（新建） | 完整分母、状态/降级分层、B3−A 配对汇总和探索性区间，不改旧指标含义 |
| `py/scripts/replay_b3_delivery_repair.py`（新建） | 旧/新确定性机制离线对照，禁止读取凭据或联网 |
| `py/scripts/run_b3_delivery_eval.py`（新建） | 独立目录、快照验证、统一配置和 360 个计划键；复用现有 runner |
| 现有 context/pipeline/verification/capacity/runtime 测试 | 对应回归和安全边界 |
| `py/knowpath_backend/test/test_rag_delivery_analysis.py`（新建） | 分母、失败、降级和配对统计测试 |

不修改已冻结的 `py/scripts/run_b3_repair_eval.py` 来承载新实验；旧 `scoring.py` 中单叶 Recall/MRR/NDCG 和 `family_descriptive` 统计定义保留。

## Task 0：保留当前证据及可复现基线

- [ ] 记录工作树状态，按当前内容归档所有冻结源码、依赖清单、实验配置、结果和本计划；归档排除 `.env`、凭据和无关用户文件。
- [ ] 修改源码之前验证原 freeze 和派生 freeze、数据集和合并结果哈希。数据集 SHA256：`230bf4a271ba2bc2fc2d774357caf2bc2e21b46ae5eccf7c9e190b05a1d9eeb2`；合并结果 SHA256：`d8eaddaee62140208f3f7b9b4d863cec77a2411c763d3799c1f975f0343a17a7`。
- [ ] 原 freeze 的 file_hashes 引用当前源码绝对路径；源码修改后不能再假装原路径仍可通过校验。建立单独的“原路径→归档副本”映射，逐项用原哈希验证副本；不改写原 freeze 身份。
- [ ] 新增实验目录 `py/.rag-evaluation/tree-b3-delivery-repair-40-v1`，复制隔离 SQLite；对现有 Qdrant 集合只读验证。明确继承的 manifest/索引身份与新的数据库身份，禁止复制配置后伪造绑定。
- [ ] 每模块保存局部差异，避免把此前未提交工作混入本轮修改；不执行 git reset、提交或推送。

## Task 1：按依赖闭合边界补入显式证据

**修改：** `context.py`、`pipeline.py`。**测试：** `test_rag_context.py`、`test_rag_pipeline.py`、`test_rag_capacity_admission.py`。

- [ ] 先添加当前会失败的用例：基线 `[main_a, main_b, definition_a, definition_b]` 中两两成组，缺失的显式锚点应插在 `definition_a` 前，而不是 `definition_b` 前。

```python
def test_missing_anchor_precedes_entire_last_group():
    from knowpath_backend.learning.rag.context import preserve_baseline_with_anchors
    rows = [
        dict(chunk_id='m1', source_text='主要条件', evidence_group='main'),
        dict(chunk_id='m2', source_text='条件续文', evidence_group='main'),
        dict(chunk_id='d1', source_text='定义开头', evidence_group='definition'),
        dict(chunk_id='d2', source_text='定义续文', evidence_group='definition'),
        dict(chunk_id='named', source_text='第四十条 点名要求', evidence_group='named'),
    ]
    result = preserve_baseline_with_anchors('结合第四十条回答', rows, rows[:4])
    assert [r['chunk_id'] for r in result] == ['m1', 'm2', 'named', 'd1', 'd2']
```

- [ ] 边界定义为最大的 `k < len(B)`，使 `B[:k]` 对正向 `requires` 和全候选的 `evidence_group` 成员闭合；`k=0` 时不替换唯一主证据组，直接保留原结果。
- [ ] 从完整 `rows` 建组，不能只看 `B`。有向依赖不得无向化。以下纯函数给出预定边界算法，测试同时用直接闭包求解作独立参照：

```python
def _closed_prefix_boundary(rows, baseline_rows):
    baseline_ids = [r['chunk_id'] for r in baseline_rows]
    n = len(baseline_ids)
    pos = {cid: i for i, cid in enumerate(baseline_ids)}
    by_id = {r['chunk_id']: r for r in rows}
    if len(pos) != n or len(by_id) != len(rows):
        raise ValueError('duplicate context identity')
    if any(cid not in by_id for cid in baseline_ids):
        raise ValueError('baseline identity missing')
    groups = {}
    for row in rows:
        if row.get('evidence_group'):
            groups.setdefault(row['evidence_group'], set()).add(row['chunk_id'])
    far, boundary = -1, 0
    for index, cid in enumerate(baseline_ids):
        row = by_id[cid]
        needed = set(row.get('requires', ())) | groups.get(row.get('evidence_group'), set())
        far = max(far, index, *(pos.get(dep, n) for dep in needed))
        k = index + 1
        if k < n and far < k:
            boundary = k
    return boundary
```

- [ ] 缺失锚点取首次 `omitted_chunk_ids` 与显式匹配的交集，并排除 `invalid_chunk_ids`。排序为 `B[:k] + missing_anchors + B[k:] + remaining`，每个 ID 恰好一次，各部分内部顺序稳定。
- [ ] 沿用原装配器最多再试一次。仅当受保护前缀在新结果开头完整保留、原已选显式锚点全保留、至少新增一个有效锚点、所有容量检查通过时采纳；否则保留第一次 context 和对应 trace。
- [ ] 诊断原因限定为 `accepted`、`no_missing_anchor`、`no_preservable_prefix`、`invalid_dependency`、`capacity_rejected`、`protected_context_not_retained`、`existing_anchor_not_retained`。不吞内部异常；准入失败与依赖无效分别报告。
- [ ] 测试有向依赖正反顺序、跨组依赖、循环、非连续同组成员、多个锚点、唯一不可分块、锚点过大/缺依赖、第二遍没有收益、原文输入不可变。对有限小图枚举核对闭合边界。

**验收：** 第 09 题离线恢复第四十条；全部所选依赖完整，原预算不变，没有新检索/模型调用。其他题如有必要证据回退，逐题解释并停止在线阶段，不能以净平均抵消掩盖回退。

## Task 2：保留本请求此前可信的部分答案

**修改：** `verification.py`。**测试：** `test_rag_verification_v2.py`、`test_rag_pipeline.py`、`test_rag_runtime.py`。

- [ ] 先把审查中的最小复现写为正式回归：第一轮一条可信结论、另一要点未完成；第二轮仅将 `missing_points` 换成错误对象类型，预期恢复旧结论并为 `partial`，不是返回空答案。

```python
# 放入现有 test_rag_verification_v2.py，复用该文件的 draft/verdict/RealLikeModel/run。
def test_terminal_contract_error_preserves_verified_partial():
    first = draft()
    first['required_points'].append(dict(point_id='p2', text='尚未完成的补充要点'))
    good = verdict('answer_incomplete')
    good['missing_points'] = ['p2']
    good['requirement_checks'].append(dict(point_id='p2', status='undetermined',
        citation_ids=[], claim_ids=[]))
    bad = copy.deepcopy(good)
    bad['missing_points'] = [{'point_id': 'p2'}]
    result = run(RealLikeModel([first, first]), RealLikeModel([good, bad]))
    assert result['status'] == 'partial'
    assert len(result['claims']) == 1
    assert result['claims'][0]['text'] == first['claims'][0]['text']
    assert result['citation_ids'] == [SOURCE['chunk_id']]
    assert result['trace']['delivery']['reason'] == 'verification_contract_failed'
    assert result['trace']['delivery']['recovered'] is True
```

- [ ] 在引用身份、限定、数字来源、依赖裁剪和 requirement 降级全部完成后，深拷贝可信子集及关联 draft/verdict 索引、规范要点清单和输入身份。快照只放在当前 `answer()` 局部变量中。
- [ ] 生成清单采用“候选→完整验证→提交”顺序。当前 `required.update` 早于引用身份检查的问题一并修复，失败输出不能污染 canonical checklist。
- [ ] 首版只允许当前终局 `safe_nonanswer_fallback` 路径中的真实 `ValidationError` 和允许的 `_ContractViolation` 触发恢复。不得仅依据 `schema_rule` 判断；普通 `RuntimeError` 不能因默认分类被当作格式错误。
- [ ] 恢复条件：相同问题与证据快照、非空可信子集、没有合法新增必要要点、没有后续有效语义判定、仍在 deadline 内。整体恢复旧快照，原样输出；不混入失败轮文字，不按重复的 `c1` ID 跨轮拼接。
- [ ] 第二轮完整合法结果始终优先，即使更少结论、明确否定、`undetermined`、`insufficient` 或 `clarify`。合法生成新增必要要点而核验失败时，保守禁止恢复旧快照；不做自由文本启发式判断。
- [ ] 无可信快照时继续空答案降级；超时、取消、scope/版本变更、预算、认证、内容拒绝、服务错误以及内部编程错误继续按原错误语义传播。检查器中泛化 `except Exception` 不得把普通内部错误转为安全空答案或可信部分答案。
- [ ] 恢复前再次检查 deadline，返回后仍执行 pipeline 的 scope/cancel/deadline guard；引用照常从本请求授权 context 构建。
- [ ] 测试首轮即失败、首轮数字/限定/依赖检查全失败、后轮有效否定、后轮新限定、相同 ID 不同结论、失败清单不污染、调用间不串快照、错误不被吞、总调用数不增加。

**验收：** 恢复内容与旧可信快照逐字段相同；不复活被有效否定的内容；失败 journal 不变；不把恢复 partial 当成 answered。真实旧记录缺少被拒绝的原始 JSON，故注入复现不能冒充在线恢复率。

## Task 3：明确降级与交付原因，补齐可定位诊断

**修改：** `verification.py`、`schema_diagnostics.py`、`diagnostics.py`，对应测试。

- [ ] 公共状态枚举保持兼容，在 `trace.delivery` 写持久的交付原因：`semantic_result`、`generation_contract_failed`、`verification_contract_failed`，另列 `recovered`、`recovery_decision`、有效快照索引和保留结论数。
- [ ] `recovery_decision` 使用固定枚举：`not_applicable`、`restored`、`no_verified_snapshot`、`requirements_changed`、`input_identity_changed`。原始失败阶段和 journal 保留，不伪造新的成功 verdict。
- [ ] 给 `citation_relationship` 增加固定细分类：`check_set_mismatch`、`check_sources_out_of_scope`、`supported_without_citation`、`supported_citations_differ_from_draft`；给要点错误区分 `requirement_set_mismatch`、`requirement_reference_invalid`。分类来自触发分支，不记录 provider 正文、Key、请求头或任意字段值。
- [ ] 对各终局分支先写测试再实现。证明字段类型错误、引用不变量错误与真实证据缺失的交付原因互不混淆；序列化/脱敏不会丢新字段。
- [ ] 契约失败无可用内容时显示“本次回答未完成可靠核验，请重试”；恢复时显示“以下为已核验的部分内容，其余部分暂未完成可靠核验”。真正 `evidence_missing` 仍说明证据不足。

**本轮不做：** 自动补空引用、宽泛删除未知字段、放松 supported 条件、增加重试次数。第 14/20 题原始失败 JSON 未保留，不能为提高通过率猜测规范化规则。

## Task 4：离线全面验收与独立审查

- [ ] 新脚本从归档基线读取并先复现 120 条原上下文，再对相同保存排序执行新装配。金标准只在选择完成后评分，不进入新规则。
- [ ] 输出每题每模式的前后来源 ID/顺序、必要证据增减、锚点增减、完整依赖、容量和补救原因；禁止只展示第 09 题。重点检查第 25 题的原收益未被破坏。
- [ ] 用真实已保存 draft/verdict 检查快照资格；将可直接验证的事实与合成错误注入分别标注。没有原失败 JSON 的地方明确“不支持精确原响应重放”。
- [ ] 执行定向测试，再执行原有 RAG 回归选择；结果数以新执行为准，不引用此前的 701 通过作为本次通过证明。

命令在项目 `py` 目录执行，`$testRunId` 使用唯一目录，避免 pytest 清理旧结果：

```powershell
$testRunId = [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -m pytest knowpath_backend/test/test_rag_context.py knowpath_backend/test/test_rag_pipeline.py knowpath_backend/test/test_rag_verification_v2.py knowpath_backend/test/test_rag_capacity_admission.py knowpath_backend/test/test_rag_token_budget.py knowpath_backend/test/test_rag_runtime.py -q -p no:cacheprovider --basetemp=.rag-evaluation/delivery-focused-$testRunId
& .\.venv\Scripts\python.exe -m pytest knowpath_backend/test -q -k rag -p no:cacheprovider --basetemp=.rag-evaluation/delivery-regression-$testRunId
```

预期：新增回归在旧实现下先失败，修改后通过；定向及完整选择无失败，skip 逐项说明。完成后独立审查装配不变量、快照安全、隐私日志和统计分母。任何未解释回退或保护检查失败均阻止在线阶段。

## Task 5：冻结分析方法，执行统一的 360 条真实测评

### 指标实现与测试

- [ ] 新建 `delivery_analysis.py`，保留历史指标名与定义。主表把“最终必要证据覆盖”明确标为“最终输入上下文必要证据覆盖”，避免被误读为答案正确率。
- [ ] 固定四层：候选联合 Recall@K、重排联合 Recall@K、输入上下文覆盖、答案引用叶子覆盖；K 为 1/3/5/10/20/40，旧单叶 MRR/NDCG 继续并列。
- [ ] 每模式计划分母 120，每题 3 次先平均后跨 40 题宏平均。全计划交付覆盖在服务失败/未交付时计 0；已观测的纯检索指标另列 measured/unknown 分母，不能把后续失败但缺轨迹当成“检索为零”。
- [ ] 分别统计流程完成率、answered/partial/insufficient/clarify/failed、有非空可信内容的交付率、发生过契约错误的请求率、终局契约降级率、恢复 partial 数、契约失败空答案数。`answered` 不是正确率，恢复也不能抹掉契约失败。
- [ ] 读取成功和失败的 journal 统计调用、token、时延和花费；缺失 usage/价格保持 unknown，不写成 0 元。
- [ ] 输出 B3−A 为主要比较、B2−A 为次要比较。新增独立统计 sidecar，冻结 `seed=20260923`、10,000 次配对 bootstrap、95% 区间；先每题平均 3 次，再以 family 为簇一起抽取各模式，不把 120 次当成 120 道独立题。当前 40 家族各一题，区间仅属已见开发题的探索性描述，不证明新资料泛化。
- [ ] 现有 `family_descriptive` 报告保持原意；新分析器及 sidecar 一起纳入新 freeze，不仅修改一个 statistics 字符串就声称已有 CI。
- [ ] 测试全相等差值为 0、手算已知配对差、失败/缺失分母、重复记录拒绝、相同种子复现、重复不改变独立题数、负向差值、恢复答案仍计契约失败、质量未知不变成成功。
- [ ] 分析器实现后，在 `py` 目录执行下列新增测试；通过后才冻结统计代码和进入在线阶段。若集成同时修改了业务路径，还须重跑 Task 4 的相关回归。

```powershell
$analysisTestRunId = [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -m pytest knowpath_backend/test/test_rag_delivery_analysis.py -q -p no:cacheprovider --basetemp=.rag-evaluation/delivery-analysis-$analysisTestRunId
```

### 在线安排

- [ ] 在运行前完成新配置、统计计划及全部代码冻结：原数据/原文/索引/manifest 哈希、隔离数据库身份、模型/协议、240/120 超时、重复 3 次、轮转顺序、停止规则均固定。
- [ ] 复用当前 runner：`repeats=3`、modes=`a,b2_r1,b3_unit`，总计 **40 × 3 × 3 = 360 个端到端请求**。每题三模式在三个重复中各占一次执行位置，不按已见分数调整顺序或次数。
- [ ] 360 个请求最多含 360 embedding、360 rerank、720 生成、720 核验调用，不是总计仅 360 次供应商 API 调用。240 秒时限下，串行请求时限合计约 24 小时，另有编排开销；这只是上限而非预计工期。金额仅按可验证价格与 usage 计算。
- [ ] 执行前验证可用凭据但不打印，主工作区 `.env` 只在进程内加载。只对既有 DashScope 端点认证，禁止凭据进入提示、报告或日志。
- [ ] 每个计划键只执行一次；超时和契约失败照实保留，不按低分选择性重跑。当前 runner 无断点续跑；如中断，报告已完成/失败/缺失，不覆写、不隐式 resume、不伪称完整。后续若需新批次，单独冻结并保留批次身份。
- [ ] 只因冻结漂移、权限/凭据问题、预算硬限制或用户停止而停止整个实验；不因 B3 分数低或某题失败提前调整算法。已有调用都记入日志。
- [ ] 执行前后验证数据和索引身份、360 个唯一计划键、重复数、每模式相同参数及实际调用数。
- [ ] 离线记录首次生成请求体的指纹，按“相同输入/不同输入”诊断引用差异。即使输入相同也不复用一次模型答案代替正式独立执行。

**可解释范围：** 默认 360 条可比较“修复后同配置的 B3 与 A/B2”，不足以证明修复前后在线提升的因果关系，也不保证 3 次重复足以确认小差异。历史 128 次尝试与混合超时合并结果只作背景。若另需严格旧/新代码因果对照，应在执行前另定同期交错矩阵；默认不自动扩展为 600 或 720 条。

## Task 6：结果解释与交付

- [ ] 第一份表：全部 40 题、三次重复的三模式总体与逐轮指标，明确每项分母、未知数和区间。
- [ ] 第二份表：每题证据从候选→重排→上下文→引用的增减；上下文装配修复收益与生成/核验恢复分开归因。
- [ ] 第三份表：系统状态、契约错误、保留 partial、空答案、失败、P50/P95、token/费用和调用数；P95 用全体实际请求而不是只选成功请求，缺执行则完整门槛未知。
- [ ] 保留最近批准的核心门槛：B3 相对同配置 A 的输入上下文必要证据覆盖至少 +3 个百分点、负例不退化、P95 不超过 A 的 1.2 倍。旧结构子集门槛及标签审查仍完整报告，不因引用上涨改换采纳指标；负例冻结标签和原文结构审计侧表均展示，不悄悄删除第 03/10 题。
- [ ] 人工答案正确性仍使用独立外部审查；未完成时明确 unknown。准备去模式标签的盲审包，但不把模型自评称为人工评分，也不以坐标覆盖替代必要要点的语义正确性。
- [ ] 结论允许“修复已通过，但 B3 收益不足/不确定”。只有达到既定指标且质量审查完成，才提出采纳建议；不自动切换默认模式。
- [ ] 交付报告、完整 JSONL、统计 JSON、冻结清单、离线差异和测试记录。原始结果保留。先让用户看分析，之后再决定是否按模块提交并推送。

## 完成标准与执行次序

执行次序为：**基线归档 → 装配修复与快照修复（可并行开发）→ 降级诊断集成 → 离线重放与回归 → 冻结统计和配置 → 360 条真实测评 → 解释结果**。

不得用“请求全部返回”代替完成标准。代码完成须证明依赖/来源/权限/时间保护仍有效；测评完成须有全部计划键、失败及未知的完整记录；B3 是否值得使用取决于新结果，不预设结论。

