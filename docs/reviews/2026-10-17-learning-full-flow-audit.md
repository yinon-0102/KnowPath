# KnowPath 学习全链路审计

日期：2026-10-17。范围：从上传指定 PDF、发布知识、选择范围、生成计划、阅读与讲解、逐题测评，到最后一个节点、学习笔记和复测。**本轮完成诊断与方案设计，未实施业务代码修复。**

配套文档：[修复设计与实施计划](C:/Users/27202/Desktop/KnowPath/docs/superpowers/plans/2026-10-17-learning-full-flow-repair.md)。

## 结论与实测结果

用户提出的三类问题均成立，但根因不只是页面缺按钮：当前系统把原文片段当成学习知识单元，再把会话、测评、任务完成、掌握和笔记完成分别维护，主学习路径缺少统一衔接。

最优先处理的事项是：有效知识单元与来源范围、测评后的确认与下一节点、作答草稿保存、进度缓存一致性、独立测评的解析时机，以及可从服务端恢复的测评入口。

| 项目 | 实际结果 |
| --- | --- |
| 测试文件 | `C:/Users/27202/Desktop/java_basic_to_condition_guide.pdf`，15页 |
| 文档本来具有的结构 | 基本数据类型、变量类型、命名规则、修饰符、运算符、循环、条件语句，7个主章节；另有封面目录与总结 |
| 系统解析与图谱 | 22个原文片段、21个主题；全部为一级 concept，无父节点、无关系边 |
| 主计划 | 21个任务，最终全部通过现有任务管理入口手工确认完成 |
| 主计划测评 | 21个节点均有 completed 测评，105次作答；另外保留2次出题校验失败记录，之后重试成功 |
| 105次作答的判定 | 20 correct、45 incorrect、40 unverified；88条带 assisted 标记 |
| 额外边界验证 | 自适应诊断、独立复测各完成一轮，各5题；验证新标签页恢复与实际请求类型 |
| 学习笔记 | 21章均已生成；21章状态仍为 IN_PROGRESS |
| 长期掌握 | 21个主题均有状态记录，mastered 为0；本轮未等待24小时间隔，也没有把测试答案当成真实掌握证明 |
| 现有测试基线 | 相关前端111项、后端95项通过；不是全仓回归结果 |

测试答案主要用于验证交互、判分、关联和状态流转，并不代表学习效果。正常路径不自动推进时，审计通过现有任务确认入口继续，未直接改数据库、伪造分数或修改时间。主计划最后的21/21是**任务确认完成**，并不表示105题全部答对或所有主题已掌握。

### 环境与数据范围

已启动现有 MySQL、Neo4j、Qdrant、MinIO 容器，以及 API、前端、graph worker、model worker。使用真实模型服务，未替换为假出题数据，未清空卷或重建数据库。

- 前端：[本地应用](http://127.0.0.1:5173)。API：`http://127.0.0.1:8000`。
- 主测试空间：`9e98ba69-0261-434e-b745-83f03c808ac6`，名称“全链路诊断｜Java基础｜2026-10-17”。
- 主计划：`d2789f30-874c-478a-a1b2-ed34f1a7c12e`。
- 测试资料：`a778adc9-e250-404c-afe7-ddb1f6336b15`；资料版本：`e328bab7-d0cc-47a6-a8a3-bc3bdd792486`；知识修订：`056b6af2-b235-47e3-948a-38b26ae848c8`。
- 边界验证空间：`cf197be2-4c08-4216-964d-7906f1f6000e`。通过 API 准备范围后，诊断与复测仍经真实页面操作。
- 原有空间和资料未用于测试写入。仓库原有未提交修改保留；本轮新增报告、计划及隔离的诊断证据。

## 用户旅程

| 阶段 | 用户希望发生的事 | 实际体验 | 关联问题 |
| --- | --- | --- | --- |
| 上传 | 知道文件是否完整、何时能开始学习 | 显示“已就绪”，但知识还未发布；需要自行找到审核入口 | R03、S02、S05 |
| 提取知识 | 得到有名称、范围、目标的知识点 | 出现目录、页脚、纯数字、跨页残句和长段落标题 | R01、R02、S08 |
| 确认范围 | 默认覆盖有效内容，或按章节快速选择 | 21个主题默认全不选，纳入与排除各列一遍，无批量操作 | R04、S03、S04 |
| 生成计划 | 看懂总量、当期安排和未安排原因 | 初始420分钟；默认7天×30分钟只有210分钟，后续节点显示内部码 | R12 |
| 阅读与提问 | 知道本节点学什么、何时结束 | 有原文页码与范围提示，但范围名称本身是残句，缺学习目标与完成标准 | R01、R05 |
| 讲解与问答 | 有来源、能区分原文和补充 | 正常节点可生成讲解；原文问答和讲解追问成功，能区分资料与模型补充 | 已验证可用；独立性提示见R08 |
| 作答 | 能持续思考、提交和恢复 | 心跳会重建页面并清掉未提交选择；新标签页缺继续入口 | R06、R11 |
| 测评汇总 | 看懂结果，并继续下一个节点 | 有结果与笔记入口，缺本节点确认完成并继续的主操作 | R05、R08—R10、R14 |
| 确认任务 | 完成后进度立即一致 | 保存成功后路线显示0/0、尚未生成计划 | R07、R15 |
| 全部结束 | 看到整轮总结、待核验与复习安排 | 21/21后仍打开上次选中的已完成节点，笔记全部“进行中” | R10、R13 |

## 问题登记：15项已复现

P1表示会阻断主流程、丢失作答意图、破坏知识边界或测评解释；P2表示需要绕路、容易误解或影响持续使用。未发现需按数据毁损或安全事故处理的P0证据。

### R01 · P1 · 原文片段直接成为学习节点，边界和可测性失真

实测节点1是封面目录；节点4、9、14分别是“Java 基础语法学习讲义 3/15、7/15、10/15”。这些节点也被分配学习、作答时间。节点4的题目包括“该讲义当前所处的页码位置是？”和“共多少页？”。节点9、14首次生成均出现 `QUESTION_VALIDATION_FAILED`，重试后又能生成文档信息题。

这不是取消题目校验可以解决的问题。无意义题目即使通过结构校验，也不应进入学习计划。跨页正文另被切成“尽早发现遗漏初始化的逻辑…”“具帮助你。重构名称前…”等独立节点。

根因：[PDF解析回退](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/materials/service.py:320)、[仅针对法律章条的结构提取](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/materials/pdf_structure.py:34)、[逐片段建主题](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/relations.py:29)。普通教学 PDF 没有识别出章节结构时，回退到按页与空行切片，再把每片当主题。

修复方向：区分章节容器、可学习单元和原文片段；过滤目录、页眉页脚等非学习内容；跨页归并正文；为单元提供学习目标、来源范围和可测状态。7个主章节应组织若干合理单元，**不应把修复目标硬编码为恰好7个节点**。

证据：[解析内容](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/java-material.json)、[初始计划](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/workbench-initial.json)、[页脚出题](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/first-pass/node-04.json)、[首次失败](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/first-pass/node-09.json)。

### R02 · P1 · 标题截断与身份标识耦合，导致纯数字、重名合并和结构丢失

实测有5个节点标题为 `2`、`4`、`5`、`6`、`7`；其他标题多为80字符的原文开头。以 `2.4`、`2.5` 开头的两个片段因英文句号截断而同名为 `2`，被归并到同一主题。

根因：[标题与ID生成](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/relations.py:16)：没有 section_path 时使用 `chunk.text.split('.', 1)[0][:80]`，ID又只由 material_id 和 name 决定。不同章节同叫“概述”也会碰撞；存在多个父路径时退回 parent_id=null。

修复方向：简短知识标题单独生成与审核；显示名称不能承担身份标识。使用结构路径、稳定来源锚点和版本映射维护单元身份，改名不能重置学习记录，不同父章节的同名小节不能自动合并。

### R03 · P2 · “文件已就绪”与“知识已发布可学习”的阶段没有贯通

资料库显示已就绪后，审核页仍为知识版本0、待审核；创建学习空间之前还需要发布。系统确有审核入口，问题是上传结束后的状态与下一步不够明确。

根因：[上传后导航](C:/Users/27202/Desktop/KnowPath/frontend/src/app.js:1048)、[审核页面](C:/Users/27202/Desktop/KnowPath/frontend/src/workspace-view.js:50)。修复应显示“文件接收→解析→知识准备→待确认→可学习”，并直接引导确认可学习内容、创建空间。保留发布边界，不能把 ready 一概改为自动发布。

证据：[上传后状态截图](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/01-upload-ready-unpublished.png)。

### R04 · P2 · 默认范围为空，没有学全部、全选或按章节批量选择

新空间需要手工勾选21项；纳入、排除重复展示全量长标题，无搜索、章节分组或资料级批量操作。大量内容时选择成本会继续放大。

根因：[scopeModal](C:/Users/27202/Desktop/KnowPath/frontend/src/app.js:1078)、[范围管理表单](C:/Users/27202/Desktop/KnowPath/frontend/src/workspace-view.js:100)。修复应提供默认“学习全部有效单元”和“自选章节”，排除操作作为逐项状态；保留清晰的选择计数与预估时间。自动加入前置必须可解释。

证据：[21项默认空选截图](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/03-scope-empty-21.png)。

### R05 · P1 · 会话、测评、任务确认之间断开，测评后没有明确推进

首节点5题全对，测评 completed、学习会话 finished，任务仍 pending。结果页只有返回任务、查看结果、笔记和计划调整等入口，没有“确认本节点完成并继续”。阅读页有次级“记录任务完成情况”，因此不是完全没有完成入口，而是主路径没有衔接。

默认学习选点还优先使用旧的保存选择，可能再次打开已完成节点。根因：[测评汇总渲染](C:/Users/27202/Desktop/KnowPath/frontend/src/learning.js:491)、[默认学习选点](C:/Users/27202/Desktop/KnowPath/frontend/src/study.js:146)。已有 [nextLearningStep](C:/Users/27202/Desktop/KnowPath/frontend/src/workbench.js:41)，但学习主路径未统一使用。

修复方向：明确节点的学习目标、阅读范围和本轮结束标准；结果页提供“确认本节点完成并继续”“继续巩固”。保存任务状态、刷新服务端投影、选择下一项应构成同一操作链。不能把答完或满分直接改成长期掌握。

证据：[首轮结果](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/assessment-first-result.json)、[任务仍pending](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/workbench-first-completed-still-pending.json)、[结果页截图](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/06-first-result-no-next.png)。

### R06 · P1 · 30秒活动心跳清掉尚未提交的答案

首轮测评选中o2后等待32秒，题目未变，但选项已不再选中。证据时间为09:44:46Z至09:45:18Z。对于认真思考或填写简答的用户，这会直接破坏作答。

根因：[活动事件通知](C:/Users/27202/Desktop/KnowPath/frontend/src/activity.js:20)触发学习更新，[app.js整体替换main](C:/Users/27202/Desktop/KnowPath/frontend/src/app.js:818)；渲染只回填已提交意图 pendingAnswer，没有未提交草稿。切后台、联网变化也走相同更新路径，但这两种触发尚未单独实测。

修复方向：按 assessment_id、question_id、答案修订号维护草稿，输入时保存，心跳只更新活动显示。保留提交中不可变的答案请求与幂等键，不能把草稿和已提交请求混用，也不能通过关闭心跳掩盖问题。

证据：[UI操作与时间记录](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/ui-transcript.jsonl)。

### R07 · P1 · 任务保存后学习路线错误显示0/0、没有计划

多个节点保存成功后，顶栏显示“0 / 0 项已完成”“尚未生成当前计划”；服务端计划仍存在，任务已完成。进入学习空间并重新加载后恢复。

根因：[workspace.committed回调](C:/Users/27202/Desktop/KnowPath/frontend/src/app.js:82)清空 state.plans 与 workbench，却只重新读取 spaces/materials，没有为仍显示的空间重新加载 workbench。

修复方向：只失效目标空间，并在刷新完成前保留已有成功数据和加载状态；完成写入后刷新任务、路线、累计进度。加载中与已确认无计划必须分别渲染。

证据：[保存后错显截图](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/07-progress-empty-after-confirmation.png)、[最终服务端21/21](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/workbench-all-tasks-confirmed.json)。

### R08 · P1 · 诊断/独立复测也即时披露解析，用户不清楚辅助标记的原因

首轮5题全对，后4题带 assisted，最终只有1条独立证据。同源题目在前题展示解析后被标辅助，是现有规则的刻意保护。问题在于所有测评类型都强制 `include_feedback:true`，而页面没有说明对后续独立证据的影响。

额外验证中，确实创建了 diagnostic 和 retest，二者请求都包含即时反馈；这不是仅根据页面默认选项推断。额外轮次期间还同时进行了原文/讲解操作，因此其全部 assisted 不能全部归因于逐题解析：跨标签、跨空间的活动测评也会接受同源辅助记录，这是现有服务端边界。

根因：[强制即时反馈](C:/Users/27202/Desktop/KnowPath/frontend/src/learning.js:295)、[同源后题辅助](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/assessments/feedback.py:27)、[活动测评的跨空间来源保护](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/materials/source_access.py:68)。已有测试明确要求共享来源的五题只有首题 eligible。

修复方向：练习即时反馈；诊断/独立复测默认收齐回答后披露解析。提前提示原文、讲解、问答与跨标签行为如何影响本轮独立性，结果展示具体原因。保留服务端辅助保护、反馈重放幂等和已提交答案保护，不能直接删除 assisted 规则。

证据：[首轮掌握证据](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/mastery-after-first.json)、[实际模式与请求](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/assessment-boundary.json)、[首次诊断POST记录](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/assessment-boundary-first-attempt.json)。

### R09 · P2 · 简答题判分能力在选择前没有说清楚

主流程40道简答均为 unverified。现有实现对开放题一律返回无可靠判分；这符合当前能力边界，不能靠随意调用模型打分解决。设置页却和选择题并列提供，没有提前说明其不能形成已核验的通过结果。

根因：[保守评分](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/assessments/grading.py:19)、[测评设置与汇总](C:/Users/27202/Desktop/KnowPath/frontend/src/learning.js:463)。修复应在题型选择前说明，汇总分别列出正确、错误、待核验，提供客观题补测或人工核验入口，不能把待核验当错误或已掌握。

### R10 · P2 · 掌握与笔记完成的具体缺口被隐藏

首节点有效证据均值1.0，状态仍 learning；笔记已生成但章节仍进行中。最终21/21任务、21章已生成、0章完成，页面没有清楚解释分别缺什么。

后端掌握规则要求至少3个独立题族、2次测评、24小时间隔、应用题证据，以及分数门槛。笔记章节完成又要求任务完成且相关主题均 mastered。这些规则有依据，问题是用户看不到足够具体的缺口和可执行下一步。

根因：[掌握门槛](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/assessments/mastery.py:12)、[证据充分性输出](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/assessments/review_policy.py:93)、[笔记章节完成](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/notes/service.py:120)、[前端掌握概览](C:/Users/27202/Desktop/KnowPath/frontend/src/learning.js:525)。

修复方向：同时显示“本轮任务已完成”“笔记已生成”“掌握证据尚不足”，逐项列出缺少的题族、次数、间隔和应用题，并给出可参加补测的时间。

证据：[最终笔记](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/final-notebook.json)、[最终学习状态](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/final-state.json)。

### R11 · P1 · 新标签页没有服务端在途测评的恢复入口

两次新标签验证中，服务端分别保留已答1题、4题的 in_progress 测评；新标签完成知识加载后，仍显示“还没有开始诊断”和“开始测评”，无继续入口。服务端答案在，恢复指针依赖当前标签的 sessionStorage。

任务记录对尚未完成的测评也只提供结果链接；结果页会拒绝未完成测评，因此不能从记录正常继续原测评。

根因：[标签内测评描述符](C:/Users/27202/Desktop/KnowPath/frontend/src/learning.js:5)、[工作台未返回活动测评](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/plans/workbench.py:157)、[记录页结果链接](C:/Users/27202/Desktop/KnowPath/frontend/src/progress.js:191)、[结果页限制](C:/Users/27202/Desktop/KnowPath/frontend/src/workspace.js:131)。

修复方向：服务端可列出未完成测评，工作台返回可恢复项；学习路由接收 assessment_id，支持生成中、作答中、评分中的同一轮恢复。不能每次新标签都自动生成新一轮，也不能把问题描述为答案物理丢失。

证据：[加载完成后的新标签快照](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/assessment-boundary.json)、[截图](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/10-new-tab-no-resume.png)。

### R12 · P2 · 预算原因暴露内部码，学习/作答阶段被称为多个时段

初始21任务各20分钟，总计420分钟，默认窗口容量210分钟。11个节点未全部排入窗口，路线直接显示 `window_capacity`。已有同一天学习与作答两条 allocation，也被显示成“分2个时段安排”，但数据并没有定义两个具体时间段。

根因：[路线排期文案](C:/Users/27202/Desktop/KnowPath/frontend/src/progress.js:108)、[容量原因](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/plans/rolling.py:139)。

修复方向：区分整个范围的工作量和本期可执行量，显示“本期尚余X分钟未安排”，提供延长窗口、增加预算、缩小范围的操作。按日期和阶段解释 allocation，不把阶段条数当成时段数。默认时长可以存在，但必须基于有效单元，不能对页脚和正文一律赋同样的学习量。

### R13 · P1 · 全部任务结束后，主学习页没有整轮总结

服务端确认21/21、无活动会话后，默认学习页仍打开上次选中的节点16，显示“任务已完成”“复测此节点”。没有主动进入本轮总结，也没有统一列出测评完成、待核验、未掌握与到期复习。

根因同默认选点及主流程衔接：[study.load](C:/Users/27202/Desktop/KnowPath/frontend/src/study.js:146)。工作台已有 finished 分支，但尚未成为主学习入口的结束状态。修复应使显式回看与默认继续分开；全完成后进入总结，显式点旧节点仍可阅读和复测。

证据：[最终任务与测评台账](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/final-ledger.json)、[最终页面记录](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/ui-transcript.jsonl)。

### R14 · P2 · 专项练习与独立复测也统一显示“诊断”

实际 kind=practice 的轮次在汇总显示“本轮诊断已完成”，按钮、等待和恢复文案也多统一叫诊断。额外 retest 同样如此。用户难以理解练习、诊断、复测三种协议的区别。

根因：[diagnosticSection统一文案](C:/Users/27202/Desktop/KnowPath/frontend/src/learning.js:463)。修复应以创建时封存的 kind 显示标题、结果与反馈规则，恢复原轮次时不随当前表单设置改变名称。

### R15 · P2 · 手工误标完成后没有直接撤销入口

任务管理的状态选项只有 completed、skipped、deferred；完成节点不再提供开始学习会话。仍可浏览、提问和复测，但没有正常的“撤销本次完成、重新学习并计时”操作。

根因：[任务写入允许状态](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/plans/service.py:381)、[已完成任务不能开新会话](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/plans/service.py:441)、[阅读页操作](C:/Users/27202/Desktop/KnowPath/frontend/src/study.js:461)。

修复方向：对当前有效节点提供显式撤销完成，保留原记录和测评，不清空掌握证据；禁止借此恢复历史失效计划。手工确认、测评完成、跳过要保持不同含义。

## 代码审计确认的10项问题

以下路径已核对实现，但没有在本轮真实库中制造扫描缺页、发布冲突或基础服务失败等条件。它们不能和已复现浏览器问题混写为同一种证据。

| 编号 / 优先级 | 触发条件、问题与影响 | 根因及修复边界 |
| --- | --- | --- |
| S01 / P1 | 同一资料版本发布新的知识修订，旧空间尚未采用。前端读到最新图谱，再因与旧绑定不符而拒绝，违背保留旧知识快照的约定。 | [api.topics/graph](C:/Users/27202/Desktop/KnowPath/frontend/src/api.js:85)未传绑定的 graph_revision_id；[查询](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/queries.py:12)选该资料版本最新发布。应支持精确修订查询，继续采用空间绑定快照。 |
| S02 / P1 | 混合文字与扫描页的PDF，个别页无可提取文字会静默跳过，用户可能把不完整内容当成全书学习。纯扫描PDF已有明确拒绝。 | [PDF解析](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/materials/service.py:320)只检查全篇是否有文字。应输出逐页覆盖，区分空白页、图像页和失败页；需要OCR时明确提示，不把空白页一概当扫描错误。 |
| S03 / P1 | 范围允许排除必要前置，或关闭自动纳入；保存成功后，规划又无条件要求前置在选中集合中，生成必然冲突。已掌握前置也没有在这一步豁免。 | [范围保存](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/spaces/service.py:205)与[规划排序](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/plans/ordering.py:83)使用不同契约。保存前统一验证未满足的硬前置，并识别有效的已掌握状态。 |
| S04 / P2 | 关闭自动纳入后重新打开恢复默认开启；修改排除项后当前前端对象可能保留旧值，刷新前后不同。 | [范围写入与响应](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/spaces/service.py:216)不持久保存include_prerequisites，响应缺excluded；[前端合并](C:/Users/27202/Desktop/KnowPath/frontend/src/app.js:1704)只合并返回字段。应完整保存并返回范围策略与排除项。 |
| S05 / P1 | 图谱/向量准备最终失败时，资料仍ready、候选仍draft；审核页一直让用户“稍后刷新”，资料轮询也已停止。解析失败本身已有失败状态，不能泛化为所有失败均无处理。 | [图谱worker失败处理](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/workers/graph.py:123)未把候选变为可展示的失败终态；[审核提示](C:/Users/27202/Desktop/KnowPath/frontend/src/workspace-view.js:52)。需统一展示失败阶段、原因、重试入口，不能无限等待。 |
| S06 / P2 | 对ready资料选择“重新解析”，界面称重新处理原文件，实际直接进入stage；同版本已发布时还复制旧快照，解析器升级也不会重读原文件。 | [enqueue_ingest](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/reconciliation.py:107)、[复用快照](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/reconciliation.py:155)。应把重解析、重新建候选和重试失败任务作为不同命令。 |
| S07 / P2 | 同一基线可存在多个pending_review候选；发布新候选后，旧候选不失效，默认审核可能重新选到旧候选，提交时基线冲突。 | [候选复用条件](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/reconciliation.py:149)、[默认diff选取](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/reconciliation.py:182)、[发布](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/reconciliation.py:211)。需原子失效同基线旧候选，并提供基于新基线重建。 |
| S08 / P2 | 多层heading文档会把章节容器及其子知识点都作为可排任务，父级又聚合子级来源，容易重复学习与出题。 | [结构主题](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/relations.py:31)、[主题查询](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/knowledge/queries.py:38)、[计划候选](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/plans/service.py:146)。章节用于组织；可学习直属内容要单独覆盖，不能简单只取叶子造成导言丢失。 |
| S09 / P2 | 创建测评收到确定未创建的4xx后，用户改题型/换任务，旧createBody/createKey仍保留，修改意图可能一直重放成旧请求。超时或结果不确定时保留原请求则是正确行为。 | [start请求描述符](C:/Users/27202/Desktop/KnowPath/frontend/src/learning.js:247)使用`||=`，只对一类旧后端schema拒绝重置。应按确定拒绝、未知结果、已接收三类状态管理意图。 |
| S11 / P2 | 关联测评页先显示diagnostic，异步读取plan后无条件configure为practice；可能覆盖用户刚选的模式。当前没有证据支持“提交时显示diagnostic但实际发送practice”。 | [异步关联初始化](C:/Users/27202/Desktop/KnowPath/frontend/src/app.js:984)。先完成关联解析再开放表单，用户修改后不再被异步默认值覆盖；已完成任务只允许retest的现有约束仍保留。 |

## 1项需专项复现的风险

**S10 / P2：从活动学习会话旁路进入测评，作答后返回阅读，可能残留 assessment 暂停原因。**

静态路径：[记录页关联测评](C:/Users/27202/Desktop/KnowPath/frontend/src/progress.js:177)→[测评写入assessment_start](C:/Users/27202/Desktop/KnowPath/py/knowpath_backend/learning/assessments/activity.py:33)→[前端next解除活动监听](C:/Users/27202/Desktop/KnowPath/frontend/src/learning.js:207)。阅读端resume/reconnect只清各自原因，未必清掉assessment原因。

本轮正常“结束并测评”先关闭学习会话，没有把该旁路场景作为已复现问题。修复前应加一个保留活动会话的专项浏览器复现：答一题、返回阅读、观察pause_reasons和服务端学习时间是否继续增长；验证失败才进入修复。不能仅凭静态路径断言已经发生计时错误。

## 合理边界与尚未执行的验证

- 结束会话、累计阅读时间、一次满分，都不足以证明长期掌握；不建议放宽这些底线来制造“顺畅”。
- 题目结构校验、来源校验、辅助证据标记、幂等提交和保守的开放题判分都应保留。本轮未发现重复点击会重复评分的证据。
- 原文页码、来源定位和“本节点范围”提示已经存在；不足在于其语义单元错误，不能说完全没有范围。
- 普通PDF没有自动推断前置关系，是当前提取能力边界；本轮没有人工标注的依赖真值，不能把零关系直接列为所有依赖都漏了的已证实错误。
- 未等待24小时进行真实间隔复测，未篡改时钟；因此未验证最终mastered转移及长期复习效果。
- 未在真实库故意停掉依赖服务、制造知识更新冲突或导入混合扫描材料；对应S类事项需用隔离夹具回归。
- 浏览器遍历曾出现诊断脚本等待了不存在的隐藏字段、过早读取上一轮失败提示、过早点击禁用的“下一题”等自身问题。原始记录保留在first-pass与boundary-first-attempt中；最终统计按服务端task_id和测评状态逐项核对，已排除这些假失败与错位计数。

## 环境维护问题 E01

本机有两个可命中的Node应用路径，`start-local.ps1`把 `Get-Command node` 返回数组的Source当成单一可执行路径，正常启动失败。已使用明确解释器路径启动服务完成本轮审计，未修启动脚本。根因：[start-local.ps1](C:/Users/27202/Desktop/KnowPath/start-local.ps1:21)。后续应明确选择首个有效Node并验证版本、规范化路径；复用服务时的进程识别也需与该路径一致。

Docker Desktop已开不代表项目容器已运行，本轮恢复现有容器后存储服务健康。没有把本机命令权限或诊断截图工具的偶发失败算成产品问题。

## 证据索引与复查

证据目录：[flow-audit-20261017](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017)。

| 证据 | 用途 |
| --- | --- |
| [最终台账](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/final-ledger.json) | 21任务、23轮主流程测评、105次作答，按真实task_id归属 |
| [最终工作台](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/final-workbench.json)、[累计进度](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/final-progress.json) | 服务端任务完成与活动会话 |
| [图谱](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/java-graph.json)、[主题](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/java-topics.json) | 主题类型、标题、无层级与零关系 |
| [边界测评](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/assessment-boundary.json) | 额外诊断、独立复测、已加载的新标签恢复缺失、实际请求 |
| [完整UI日志](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/ui-transcript.jsonl) | 操作时间、DOM文本与选项、最终21/21但仍停旧节点 |
| [测试基线](C:/Users/27202/Desktop/KnowPath/.runtime.local/flow-audit-20261017/baseline-checks.json) | 两组已运行的现有测试及精确命令 |

复查主空间时可直接打开 [主学习页面](http://127.0.0.1:5173/#/space/9e98ba69-0261-434e-b745-83f03c808ac6/study)。测试数据保留，便于对照；计划和后端服务处于本次审计后的状态。
