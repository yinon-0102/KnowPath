# Keel Learning 后端接口设计

版本：v0.1-draft  
日期：2026-09-17  
协议：REST JSON + Server-Sent Events（SSE）  
基础路径：`/api/v1`

本文档定义学习助手后端的业务接口。首版按本地单用户设计，不包含账号和权限接口；接口中的资源标识仍保留，便于后续扩展多人服务。

性质：接口契约草案，尚未实现；架构和算法规则见 [ARCHITECTURE.md](ARCHITECTURE.md)。所有示例值仅用于说明，不代表真实测量结果。

存储选型已确认：业务数据采用 MySQL，知识图谱采用 Neo4j，向量检索采用 Qdrant。资源 ID 和 REST 路径保持与存储实现解耦。所有“同一事务”特指 MySQL 内部业务写入；图谱/向量的同步采用 outbox 与幂等后台任务，在就绪之前不发布版本，不能假设跨三个存储原子提交。

## 1. 通用约定

### 1.1 请求和响应

- JSON 接口使用 `Content-Type: application/json`；
- 资料上传使用 `multipart/form-data`；
- 时间使用 ISO 8601 UTC，例如 `2026-09-17T09:30:00Z`；
- 所有资源使用字符串 ID，推荐 UUID；
- 列表响应统一包含 `items`、`next_cursor`；
- 异步任务统一返回 `run_id`，通过 SSE 查询进度；
- 创建资源、追加答题/事件、启动异步任务和持久化状态变更的请求使用 `Idempotency-Key`；
- 资源状态更新支持 `expected_version`，版本冲突返回 `409`。

默认服务地址 `http://127.0.0.1:8000`。本地配置产生随机会话令牌，由调用方通过 `X-Local-Token` 发送；SSE 使用支持自定义头的流式 fetch，不把令牌放进 URL。除只返回最少信息的 health 外，接口均要求令牌；Origin 必须匹配配置白名单。没有登录/注册接口，不代表接口可以无约束公开。

GET 默认 200，资源创建默认 201，修改默认 200，无响应删除 204，后台执行返回 202。创建资源、追加答题或事件、启动异步任务以及会改变持久化状态的 POST 必须提供 `Idempotency-Key`，保留 24 小时；相同 key 和相同请求返回同一资源/run，不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。`POST /runs/{run_id}/cancel`、`POST /assessments/{assessment_id}/finalize` 和 `POST /sessions/{session_id}/finish` 由资源终态保证幂等，可以不要求 key；重复调用返回当前状态或原结果。追加型会话事件必须使用 key，或者提供客户端生成的唯一 `event_id`。分页 limit 默认 20、最大 100，next_cursor 为不透明字符串或 null。示例中的 mat_123 等 ID 只是占位值；实现统一用 UUID 字符串。请求未声明字段返回 422。

### 1.2 统一错误格式

```json
{
  "error": {
    "code": "TOPIC_NOT_FOUND",
    "message": "学习主题不存在",
    "details": {
      "topic_id": "topic_123"
    },
    "retryable": false,
    "request_id": "req_abc"
  }
}
```

常见错误码：

| HTTP | code                    | 含义                             |
| ---: | ----------------------- | -------------------------------- |
|  400 | `INVALID_REQUEST`       | 参数格式错误                     |
|  404 | `RESOURCE_NOT_FOUND`    | 资源不存在                       |
|  409 | `VERSION_CONFLICT`      | 状态版本冲突                     |
|  413 | `MATERIAL_TOO_LARGE`    | 资料超出大小限制                 |
|  422 | `UNSUPPORTED_MATERIAL`  | 文件类型或内容不支持             |
|  429 | `RATE_LIMITED`          | 模型或任务频率受限               |
|  500 | `INTERNAL_ERROR`        | 未预期的服务错误                 |
|  503 | `MODEL_UNAVAILABLE`     | DashScope 对话模型服务暂不可用   |
|  503 | `EMBEDDING_UNAVAILABLE` | DashScope Embedding 服务暂不可用 |
|  422 | `UNSUPPORTED_MODEL`     | 当前适配器不支持指定模型或能力   |


## 1.3 通用模型抽象与默认实现

业务层不直接依赖某一家模型厂商的 SDK，而是依赖统一的模型抽象。模型适配层至少提供两个接口：

- `ChatModel`：统一文本生成、结构化输出、流式输出和工具调用能力；
- `EmbeddingModel`：统一单条/批量文本向量化、维度查询和模型版本标识。

默认实现使用阿里云百炼 DashScope 的通义千问系列：

| 能力                                 | 默认 provider | 默认模型            | 默认接口地址                                        | 配置项              |
| ------------------------------------ | ------------- | ------------------- | --------------------------------------------------- | ------------------- |
| 对话、题目生成、评分辅助、计划解释   | `dashscope`   | `qwen-plus`         | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` |
| 文本向量化、语义检索、记忆相关度计算 | `dashscope`   | `text-embedding-v3` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` |

统一模型配置模板如下：

```json
{
  "chat": {
    "provider": "dashscope",
    "model": "qwen-plus",
    "api_key_env": "DASHSCOPE_API_KEY",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "timeout_seconds": 60
  },
  "embedding": {
    "provider": "dashscope",
    "model": "text-embedding-v3",
    "api_key_env": "DASHSCOPE_API_KEY",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "dimension": 1024
  }
}
```

`provider`、`model`、`base_url` 和认证信息通过适配器解析。新增模型厂商时只新增 `ChatModelAdapter` 或 `EmbeddingModelAdapter`，不得在 Agent、学习计划、题目生成、掌握度更新等业务模块中增加厂商判断。OpenAI-compatible HTTP 协议可以作为一种传输方式，但不等同于固定绑定 OpenAI 服务商。

默认配置加载顺序为：代码默认值 → 配置文件 → 环境变量。环境变量可以覆盖默认模型，但必须经过模型适配器的能力校验；不存在对应能力时返回 `422 UNSUPPORTED_MODEL`。未配置 provider 时使用 `dashscope`，未配置聊天模型时使用 `qwen-plus`，未配置向量模型时使用 `text-embedding-v3`。

模型调用失败时不得伪造答案、评分、掌握度或知识更新结果。对话生成失败返回 `503 MODEL_UNAVAILABLE`；向量化失败返回 `503 EMBEDDING_UNAVAILABLE`；服务恢复后由原异步任务按 `run_id` 重试，超过重试上限进入 `failed`，保留失败原因。
## 2. 资源状态

### 2.1 Material

```json
{
  "id": "mat_123",
  "name": "Python 基础教程",
  "version": 1,
  "type": "pdf",
  "status": "ready",
  "current_version_id": "matver_001",
  "size_bytes": 1832456,
  "created_at": "2026-09-17T09:30:00Z",
  "updated_at": "2026-09-17T09:35:00Z"
}
```

`version` 是资料资源的乐观锁版本，PATCH/DELETE 的 `expected_version` 对应该值；与 `current_version_id` 指向的内容版本分开。

`status`：`uploaded`、`processing`、`ready`、`needs_review`、`failed`、`archived`。

### 2.2 TopicNode

```json
{
  "id": "topic_001",
  "name": "函数参数传递",
  "kind": "concept",
  "parent_id": "topic_python_functions",
  "level": 2,
  "confidence": 0.92,
  "source_refs": [
    {"material_id":"mat_123","material_version_id":"matver_001","chunk_id":"chunk_018","page":42,"line_start":null,"line_end":null}
  ],
  "prerequisites": ["topic_variables", "topic_control_flow"],
  "status": "active",
  "graph_version": 3
}
```

### 2.3 LearnerState

```json
{
  "topic_id": "topic_001",
  "mastery_score": 0.42,
  "score_validity": "current",
  "topic_revision_id": "topicrev_001",
  "policy_version": "mastery-v1",
  "status": "needs_review",
  "error_tags": ["confuses_positional_and_keyword_args"],
  "evidence_count": 5,
  "last_assessed_at": "2026-09-17T10:00:00Z",
  "next_review_at": "2026-09-18T10:00:00Z",
  "state_version": 7
}
```

### 2.4 Evidence

```json
{
  "id": "evidence_001",
  "topic_id": "topic_001",
  "assessment_id": "assessment_001",
  "attempt_id": "attempt_001",
  "kind": "objective_answer",
  "result": "incorrect",
  "score": 0.0,
  "error_tags": ["confuses_positional_and_keyword_args"],
  "source_refs": [
    {"material_id":"mat_123","material_version_id":"matver_001","chunk_id":"chunk_018","page":42,"line_start":null,"line_end":null}
  ],
  "created_at": "2026-09-17T10:00:00Z"
}
```

## 3. 健康检查和异步运行

### `GET /health`

检查服务和基础依赖状态。

响应：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "runtime": "keel-learning",
  "dependencies": {
    "mysql": "ok",
    "neo4j": "ok",
    "vector_store": "qdrant",
    "llm": {"provider":"dashscope","model":"qwen-plus","status":"configured"},
    "embedding": {"provider":"dashscope","model":"text-embedding-v3","status":"configured"}
  }
}
```

### `GET /runs/{run_id}`

查询异步任务的当前状态。

响应字段：`id`、`kind`、`status`（`queued`、`running`、`succeeded`、`failed`、`cancelled`）、`progress`、`result_ref`、`error`、`created_at`、`finished_at`。

### `GET /runs/{run_id}/events`

SSE 事件流。事件示例：

```text
event: run.started
data: {"run_id":"run_001","kind":"material_ingest"}

event: tool.started
data: {"run_id":"run_001","tool":"search_material"}

event: progress
data: {"run_id":"run_001","step":"extract_topics","percent":60}

event: run.completed
data: {"run_id":"run_001","result_ref":"matver_001"}
```

SSE 只传执行状态和可展示的摘要，不传完整原始资料或敏感内容。

每条 SSE 事件均有递增的 `id:` 和服务端时间戳。对话另外发送 `message.delta`（可见文本片段）、`message.completed`（消息 ID 与引用）；验证中的候选文本不能先展示。终态事件为 run.completed/run.failed/run.cancelled。15 秒发送心跳注释，支持 Last-Event-ID 续传，7 天之前的历史返回 `410 EVENT_HISTORY_EXPIRED`，调用方改查 run 最终状态。终态后关闭流。

### `POST /runs/{run_id}/cancel`

空 JSON 请求；202 返回 `{ "id": "run_001", "status": "cancelling" }`。取消是协作式：检查步骤边界和流式模型输出，已发布事务不回滚；迟到的未发布生成结果丢弃。终态 run 重复取消返回原终态 200。无法立即取消外部模型请求时等待超时，不能虚报 cancelled。

run 对象的 result_ref 是 `{ "type": "assessment", "id": "assessment_001" }` 或 null；error 是通用 error 对象或 null。额外状态 cancelling 在上述取消期间使用。

## 4. 资料与知识图谱接口

### `POST /materials`

上传新资料并创建 `Material`。支持 `.pdf`、`.md`、`.txt`。

请求：`multipart/form-data`，字段：

- `file`：文件内容；
- `name`：可选显示名称；
- `auto_ingest`：是否上传后立即解析，默认 `true`。

响应 `201`：

```json
{
  "material": {"id":"mat_123","status":"processing"},
  "run_id": "run_001"
}
```

### `GET /materials`

查询资料列表。支持 `status`、`cursor`、`limit`。

### `GET /materials/{material_id}`

返回资料元数据、当前版本和解析状态。

### `POST /materials/{material_id}/versions`

上传同一资料的新版本。不会直接覆盖旧版本。

请求字段：`file`、`change_note`、`auto_ingest`。

响应 201：返回 `{ "material_version_id": "matver_002", "status": "processing", "run_id": "run_004" }`。解析只形成待发布图谱，不修改已绑定学习空间。

### `GET /materials/{material_id}/topics`

返回当前资料的章节树和知识点树。

查询参数：

- `version_id`：指定资料版本；
- `include_inactive`：是否包含失效节点；
- `depth`：最大层级。

### `GET /topics/{topic_id}/graph`

返回一个知识点及其前置、后继、相关知识和来源片段。

查询参数：`depth`（默认 1）、`include_sources`（默认 `true`）。

### `POST /materials/{material_id}/reconcile`

对新版本和现有知识图谱执行去重、冲突标记和索引候选生成。结果先进入待审核快照，不会自动切换学习空间正在使用的图谱版本。通常由资料导入任务内部调用，也允许用户在资料状态为 `needs_review` 时重试。

响应：`run_id`。

### `GET /materials/{material_id}/graph-diff`

返回待审核图谱快照与当前已发布快照之间的节点、关系、来源和受影响知识点差异。

查询参数：`revision_id`、`include_unchanged`（默认 `false`）。

### `POST /materials/{material_id}/graph-revisions/{revision_id}/publish`

发布已审核的图谱快照。需要提交 `expected_graph_version`；发布后，旧快照仍可查询但不会被新任务默认使用。

请求：

```json
{
  "expected_graph_version": 3,
  "resolutions": [
    {"conflict_id": "conflict_001", "action": "keep_both"}
  ]
}
```

action 枚举：keep_old/use_new/keep_both，前两者针对同一概念变更必须记录 reason；keep_both 保留矛盾并排除该断言的自动出题。未决冲突返回 `409 GRAPH_CONFLICTS_PENDING`，不能静默选择一方。成功 200 返回 graph_version、material_version_id、published_at；发布本身不自动迁移任何学习空间。

## 5. 学习空间和学习者模型接口

### `POST /learning-spaces`

创建学习空间，将资料和学习目标绑定。

请求：

```json
{
  "name": "Python 函数学习",
  "material_ids": ["mat_123"],
  "goal": "理解并能使用 Python 函数",
  "target_date": "2026-10-01",
  "weekly_minutes": 180
}
```

响应：

```json
{
  "id": "space_001",
  "name": "Python 函数学习",
  "status": "draft",
  "profile": {
    "goal": {"value":"理解并能使用 Python 函数","source":"explicit","updated_at":"2026-09-17T09:30:00Z"},
    "weekly_minutes": {"value":180,"source":"explicit","updated_at":"2026-09-17T09:30:00Z"}
  },
  "bindings": [{"material_id":"mat_123","material_version_id":"matver_001","graph_version":3}],
  "space_version": 1,
  "profile_version": 1,
  "state_version": 0
}
```

### `PATCH /learning-spaces/{space_id}`

仅修改 name（1—100 字）与 status（active/archived），expected_version 必填。目标、时间和偏好统一通过 profile 接口修改，避免双重写入口。

### `GET /learning-spaces/{space_id}`

返回学习空间、资料、当前选定主题、画像摘要和整体状态。

### `POST /learning-spaces/{space_id}/scope`

设置本轮学习范围。

请求：

```json
{
  "topic_ids": ["topic_functions", "topic_parameters"],
  "include_prerequisites": true,
  "excluded_topic_ids": [],
  "expected_version": 1
}
```

响应返回 `scope_version`、解析出的前置主题和需要用户确认的主题。

topic_ids 必须非空且来自当前绑定快照。include_prerequisites=true 表示用户同意纳入有来源、已确认且无环的前置主题；缺少或有争议的前置关系只返回 recommended_topic_ids，不扩展范围。修改范围使旧计划 needs_replan，已开始测验仍按原快照完成。

### `GET /learning-spaces/{space_id}/profile`

返回学习者模型，包括字段来源、置信度和更新时间。敏感字段不在首版模型中自动推断。

### `PATCH /learning-spaces/{space_id}/profile`

修改可编辑的学习目标、时间约束和学习偏好。请求必须携带 `expected_version`（对应 profile_version）；用户修改的字段标记为 `explicit`，不能被普通对话自动覆盖。

字段：goal（1—1000 字）、weekly_minutes（整数 15—2400）、target_date（YYYY-MM-DD 或 null）、preferences（example_first/concise_explanations，布尔值）。expected_version 必填；空字段删除可选偏好，goal 和 weekly_minutes 不可为空。系统推断存为待确认候选，不覆盖 explicit 字段。

### `GET /learning-spaces/{space_id}/state`

按主题返回掌握度、错误标签、证据数量和下一次复习时间。

查询参数：`status`、`topic_id`、`include_evidence`。

## 6. 诊断、练习和复测接口

### `POST /learning-spaces/{space_id}/assessments`

创建诊断或练习任务。

请求：

```json
{
  "kind": "diagnostic",
  "topic_ids": ["topic_functions"],
  "question_count": 8,
  "question_types": ["single_choice", "short_answer"],
  "difficulty_mix": {"easy": 0.3, "medium": 0.5, "hard": 0.2}
}
```

响应：

```json
{
  "assessment": {
    "id": "assessment_001",
    "kind": "diagnostic",
    "status": "generating",
    "topic_ids": ["topic_functions"],
    "question_count": 8,
    "material_version_ids": ["matver_001"]
  },
  "run_id": "run_005"
}
```

此接口返回 202。kind=diagnostic/practice/retest，question_count 为 5—10；首版题型仅 single_choice/short_answer，difficulty_mix 每项在 0..1 且合计 1。资料不足以生成有效题目时失败，不生成超范围凑数题。服务端冻结 scope_version、graph_versions、material_version_ids 和 assessment_policy_version。

题目公共对象包含 id、type、prompt、options（单选必有）、topic_ids、difficulty。内部 source_refs、答案键、评分要点在评估完成前不返回；生成失败或校验未通过的题目不进入题集。难度是组卷启发式标签，不是校准后的测量结果。

### `GET /assessments/{assessment_id}`

返回诊断或练习的题目、状态和已提交答案摘要。

状态：generating/ready/in_progress/grading/completed/failed/cancelled。仅 ready/in_progress 接收答题；generating 不返回题目。completed 返回结果链接，详细解析通过 result 获取。

### `POST /assessments/{assessment_id}/attempts`

提交一道题或一批题的答案。

请求：

```json
{
  "answers": [
    {
      "question_id": "question_001",
      "expected_answer_revision": 0,
      "answer": "使用关键字参数传递",
      "elapsed_seconds": 48
    }
  ],
  "finalize": false
}
```

answers 为 1—10 项；单选 answer 传选项 ID，短答传 1—4000 字文本。elapsed_seconds 可选、0—86400，只作自报辅助数据，不用于判定掌握。重复 question_id 拒绝；同一题可在 finalize 前修改，使用 expected_answer_revision 乐观锁；版本不匹配 409。每次提交追加记录，评分只用封存时最新答案。finalize=true 等同原子保存后调用 finalize，返回 202 与 run_id，而不是上述普通记录响应。

响应：

```json
{
  "attempt_id": "attempt_001",
  "status": "recorded",
  "accepted_count": 1,
  "next_question_id": "question_002"
}
```

### `POST /assessments/{assessment_id}/finalize`

结束一次诊断或练习，执行判分、证据写入、掌握度更新和计划影响分析。

请求 `{ "allow_unanswered": false }`。false 且有未答题返回 409 ASSESSMENT_INCOMPLETE；true 允许交卷，未答题记 insufficient_evidence，不按答错计。封存后答案不可修改；重复 finalize 返回原 run/result，不重复计分。评分发布在事务中完成，计划重排以单独 run 执行。

对于包含开放题但无法稳定判定的提交，结果可以为 `unverified`；此时只保存反馈证据，不提高或降低掌握度，并建议生成替代客观题。

响应：

```json
{
  "run_id": "run_002",
  "assessment_id": "assessment_001",
  "status": "processing"
}
```

### `GET /assessments/{assessment_id}/result`

返回完成后的结果，包括知识点级表现、错误标签、证据引用、是否需要人工确认和下一步建议。

不直接把模型内部思考过程作为接口字段返回。

result 响应至少包含：assessment_id、graded_at、topic_results（topic_id、topic_revision_id、score、verified_count、unverified_count、error_tags、evidence_ids）、question_results（question_id、verdict=correct/partial/incorrect/unverified、score 可空、rubric_version、feedback、source_refs）、state_version、plan_replan_run_id 可空。所有分数都是对应评分标准下的结果，不宣称能力概率。

练习与复测统一使用 Assessment，不额外建设 practice 提交资源，避免出现两套判分和证据更新入口。

## 7. 计划和学习会话接口

### `POST /learning-spaces/{space_id}/plans`

基于当前范围、学习者状态和时间约束生成计划。

请求：

```json
{
  "session_count": 5,
  "minutes_per_session": 30,
  "include_review": true,
  "rebuild_mode": "initial"
}
```

`rebuild_mode`：`initial` 或 `local_replan`。

session_count=3—5，minutes_per_session=10—120，必须满足 profile 时间约束。local_replan 另需 base_plan_id 和 expected_plan_version；仅重排未完成、受影响任务，历史任务保留。返回 422 PLAN_CONSTRAINT_UNSATISFIABLE 时 details 列出冲突及用户可调整条件。

响应：`run_id`，完成后生成 `plan_id`。

### `GET /plans/{plan_id}`

返回计划及每个任务的生成原因：

```json
{
  "id": "plan_001",
  "space_id": "space_001",
  "version": 4,
  "tasks": [
    {
      "id": "task_001",
      "topic_ids": ["topic_parameters"],
      "kind": "targeted_practice",
      "status": "pending",
      "estimated_minutes": 25,
      "reason": "该知识点掌握度 0.42，且最近两次练习存在同类错误"
    }
  ]
}
```

### `PATCH /plans/{plan_id}/tasks/{task_id}`

用户确认完成、跳过或延期任务。系统不会因为用户手动标记完成就直接提高掌握度，掌握度仍以练习和复测证据为准。

请求：

```json
{
  "status": "completed",
  "note": "已阅读并完成练习",
  "expected_plan_version": 4
}
```

可写状态仅 completed/skipped/deferred；deferred 必须有 defer_until（UTC 时间），skipped 必须有 reason。手动标记不产生答题证据。任务预计时间和自报时间不作为实际学习效果的唯一依据。

### `POST /plans/{plan_id}/sessions`

开始一次学习会话，返回会话 ID 和本次应读取的上下文摘要。

请求 `{ "task_id": "task_001" }`；201 返回 id、plan_id、task_id、status=active、started_at。不得返回隐藏题目答案或模型系统提示。一个学习空间只允许一个 active session，冲突返回 409 SESSION_ACTIVE。

### `POST /sessions/{session_id}/events`

记录非评分型学习事件，例如打开资料、请求解释、查看提示、暂停学习。事件不直接修改掌握度。

请求字段：type=open_material/request_explanation/request_hint/pause/resume、topic_id 可选、question_id 可选、client_timestamp 可选。服务端记录 received_at；request_hint 同时使相关题目独立性标记为 assisted，该作答不计入独立掌握证据。

### `POST /sessions/{session_id}/finish`

结束学习会话并返回耗时、完成任务和可选的练习建议。

空 JSON 请求，200 返回 session_id、status=finished、elapsed_seconds、reported_active_seconds（可空）、task_ids。连接时长不自动解释为持续学习时长。

## 8. 对话与 Agent 运行接口

### `POST /learning-spaces/{space_id}/messages`

在指定学习空间内发送自然语言请求，例如“用例子解释这个知识点”或“给我一道类似练习”。

请求：

```json
{
  "message": "请用一个简单例子解释函数参数传递",
  "session_id": "session_001",
  "stream": true
}
```

响应：

```json
{
  "run_id": "run_003",
  "session_id": "session_001",
  "status": "queued"
}
```

生成内容必须使用当前学习范围和资料来源。需要记录工具调用，但不向用户泄露内部提示词和隐藏状态。

首版只接受 stream=true；message 为 1—8000 字。session_id 可空，为空时创建独立对话会话，不修改计划任务状态。202 返回 run_id/session_id/status，文本由 SSE message.delta 发送。活动测验内问答案应转为提示请求并记录 assisted，禁止隐式写入成绩。

## 9. 证据和知识更新接口

### `GET /learning-spaces/{space_id}/evidence`

查询某个学习空间的答题证据、来源片段和掌握度变化原因。

查询参数：`topic_id`、`from`、`to`、`kind`、`limit`。

### `POST /learning-spaces/{space_id}/knowledge-corrections`

用户对资料解析结果或知识关系进行纠正。

请求：

```json
{
  "kind": "relation",
  "target_id": "relation_001",
  "action": "reject",
  "reason": "教材中没有这个前置关系",
  "source_ref": "chunk_018"
}
```

纠正作为新的事件保存，不直接删除原始抽取结果。后续图谱版本会标记原关系失效。

### `POST /learning-spaces/{space_id}/knowledge-corrections/{correction_id}/confirm`

确认一条用户纠正；请求 expected_graph_version 和 reason。202 返回 run_id，由内部调用同一 GraphService 发布新快照。完成后返回 graph_version、affected_topic_ids 和 update_available；仍由 `/learning-spaces/{space_id}/knowledge-updates/apply` 确认空间采用新快照，不在此入口自动切换学习状态。

### `GET /learning-spaces/{space_id}/changes`

返回资料版本、图谱变更、掌握度变更和计划重排的时间线。

## 10. 接口与 Keel 模块映射

| 接口能力                | Keel 或新增模块                                              |
| ----------------------- | ------------------------------------------------------------ |
| `/materials`            | 新增 `MaterialService`、解析器和来源索引                     |
| `/topics`、`/graph`     | Neo4j + 学习领域图谱适配层，借鉴 Keel 的时态与冲突处理机制   |
| `/profile`、`/state`    | MySQL 结构化 `LearnerRepository`，辅以可重建的 Keel Memory 摘要 |
| `/assessments`          | 新增题目生成器、题目存储和 `LearningChecker`                 |
| `/plans`                | 新增约束计划器，复用 `TodoStore` 的状态思想                  |
| `/messages`             | 裁剪后的 `FunctionCallAgent` + Context Engine                |
| `/runs/events`          | Agent 工具和 LLM 回调的 API/SSE 适配器                       |
| `/evidence`、`/changes` | 事件存储、版本追踪和可解释性服务                             |

## 11. 幂等、并发和状态规则

1. 资料上传以文件哈希和 `Idempotency-Key` 去重；
2. 答题版本以 assessment_id + question_id + answer_revision 唯一标识，封存后的评分关联固定 submission_id；
3. 掌握度更新以证据 ID 幂等，重复消费不会重复加分或扣分；
4. 计划更新必须带 `expected_plan_version`，冲突时返回最新计划而不覆盖用户修改；
5. 资料版本不可变，知识图谱变更创建新 `graph_version`；
6. SSE 断线后可以使用 `Last-Event-ID` 继续读取，最终状态以 `GET /runs/{run_id}` 为准；
7. 用户手动完成任务不会伪造学习证据，系统必须区分“任务完成”和“知识掌握”。

8. 资料图谱采用 `draft → pending_review → published → superseded` 状态流转；未发布快照不能被新题目和计划使用。
9. 学习空间确认采用资料/图谱更新后，未受影响的知识点可沿用证据；受影响知识点必须标记 score_validity=stale，直到新版本下重新验证。旧分数仅供历史回看。

## 12. 首版验收标准

- 可以上传并解析 PDF、Markdown、TXT，并返回章节和知识点来源；
- 可以建立学习空间并选择学习范围；
- 可以生成有知识点映射和资料来源的诊断题；
- 提交答案后能够保存证据并更新知识点状态；
- 可以生成计划，并在复测失败后进行局部调整；
- 资料新版本不会静默覆盖旧图谱；
- API 不提供 Shell 或任意代码执行入口；
- 模型不可用时不会伪造掌握度、答案或验证结果；
- 关键异步任务可以通过 SSE 观察，并能查询最终状态；
- 关键状态变更可以通过证据和版本接口追溯。

## 13. 版本采用、来源与数据管理接口

以下为前面业务流程所必需的配套接口，所有路径仍以 `/api/v1` 为前缀。表中必填字段未标“可选”时均必填。

| 方法与路径                                                   | 输入                                                         | 输出与语义                                                   |
| ------------------------------------------------------------ | ------------------------------------------------------------ | ------------------------------------------------------------ |
| GET `/learning-spaces`                                       | cursor、limit 可选                                           | 200 items、next_cursor；列出已有空间                         |
| GET `/materials/{material_id}/versions`                      | cursor、limit 可选                                           | 200 版本列表，每项含 id、content_hash、status、graph_version、created_at |
| POST `/materials/{material_id}/ingest`                       | version_id                                                   | 202 run_id；auto_ingest=false 或解析失败后的显式启动，不覆盖旧已发布版本 |
| GET `/materials/{material_id}/versions/{version_id}/chunks/{chunk_id}` | 无                                                           | 200 text、page/line_start/line_end、section_path、content_hash；明确来源。若当前学习空间有活动测验，调用必须携带 space_id，测验服务限制题目引用；用户主动查看源材料时标记相关作答 assisted |
| GET `/learning-spaces/{space_id}/knowledge-updates`          | 无                                                           | 200 bindings、available_updates、affected_topic_ids、invalidated_question_ids、plan_impact；只预览，不修改状态 |
| POST `/learning-spaces/{space_id}/knowledge-updates/apply`   | bindings（material_id、material_version_id、graph_version 数组）、expected_space_version | 202 run_id；只采用已发布快照，同一事务替换空间绑定、标记受影响状态 stale 和旧计划 needs_replan。未变化概念证据可复用，待重排计划另有 run_id |
| POST `/learning-spaces/{space_id}/state/reset`               | topic_ids（非空）、expected_state_version、reason            | 200 state_version；追加 reset 事件，选定主题从新证据周期重新评估，历史保留；旧证据不再参与新状态计算 |
| POST `/assessments/{assessment_id}/grade-reviews`            | question_id、reason                                          | 202 run_id；对评分提出复核，仍用原封存答案和原评分标准。结果不确定则 unverified；撤销旧证据并重算，不能重复加分 |
| POST `/learning-spaces/{space_id}/exports`                   | format=json                                                  | 202 run_id；导出空间目标、学习记录、版本清单及有权查看的资料引用，不导出 API Key 或系统提示 |
| GET `/exports/{export_id}/download`                          | 无                                                           | 200 application/zip；仅服务端生成的导出 ID，可下载 24 小时，过期 410 |
| PATCH `/materials/{material_id}`                             | status=archived 或 name、expected_version                    | 200 Material；归档不清除证据，禁止新空间绑定                 |
| DELETE `/learning-spaces/{space_id}`                         | expected_version、confirm=true                               | 202 run_id；清除该空间记录、派生状态及记忆，保留其他空间共享资料 |
| DELETE `/materials/{material_id}`                            | expected_version、confirm=true、cascade（默认 false）        | 202 run_id；被空间引用且 cascade=false 时返回 409 MATERIAL_IN_USE 和影响空间。cascade=true 才清除原文件、解析/图谱版本、索引、来源相关题目和证据并重算受影响状态 |

删除和重置是不同操作：reset 保留学习历史，delete 彻底清除约定范围。运行中的相关任务先取消，防止数据被迟到结果重新写入；无法清除的文件返回 failed 与原因，不虚报成功。首版本地文件清除不承诺硬件级安全擦除。

版本采用示例请求：

```json
{
  "bindings": [
    {"material_id":"mat_123","material_version_id":"matver_002","graph_version":4}
  ],
  "expected_space_version": 3
}
```

run 完成结果包含 space_id、space_version、affected_topic_ids、stale_state_count、plan_replan_run_id。所有后台生成任务都记录启动时的绑定版本，提交结果时发现已变化则标记 STALE_INPUT，保留结果供历史查看，但不得更新新快照的掌握度或计划。

## 14. 关键响应契约与限制

### 14.1 图谱与纠正

TopicNode 和图谱边响应增加 revision_id、material_version_id、graph_version、valid_from、valid_to（可空）、recorded_at。GET graph 返回 `{nodes:[],edges:[],graph_version:3}`，depth=1—3。边字段为 id、revision_id、from_id、to_id、type、status、source_refs、confidence。章节 contains 和学习 prerequisite_of 分开处理；发布前 prerequisite_of 必须无环。

graph-diff 返回 base_graph_version、candidate_revision_id、added/changed/removed 数组、conflicts 数组和 affected_topic_ids。reconcile 请求包含 version_id、expected_graph_version，首次发布基版本为 0。候选图谱与索引由 worker 准备就绪，再允许 publish 在 MySQL 中原子切换发布状态；未就绪返回 409 REVISION_NOT_READY。只有自动规则确认的无冲突首次提取可直接 ready；其他结果 needs_review，等待 publish。knowledge-corrections 请求 kind=node/relation、target_id、action=reject/replace、reason、source_ref；replace 需要 proposed_value。201 返回 correction_id、status=pending、candidate_revision_id。confirm 不改变学习空间绑定。

### 14.2 版本字段的含义

- expected_version：当前 PATCH/DELETE 资源的乐观锁版本；不与知识图谱版本混用。
- space_version：空间成员、绑定和选择范围的并发控制版本；创建返回 1。
- scope_version：用户已确认主题范围的版本。
- profile_version：目标、时间预算和偏好的版本。
- state_version：空间内掌握度派生状态的递增版本。
- plan version：每份持久化计划快照版本。
- graph_version：每份资料下的已发布图谱版本；多资料使用 bindings/graph_versions 数组，不能使用一个全局数字代替所有版本。

创建空间的 material_ids 自动绑定各自当前已发布快照；任一资料没有已发布快照则返回 409 MATERIAL_NOT_READY。返回中包含 bindings、space_version=1、profile_version=1；尚未评估的 state_version=0。profile 中每个字段带 value、source=explicit/inferred、updated_at，推断字段另带 confirmation=pending/confirmed。

### 14.3 上传、内容和错误约束

上传单文件最多 20 MiB、文本 PDF 最多 300 页、每空间最多 5 份资料；Markdown/TXT 使用 UTF-8。校验实际内容而非仅后缀。加密 PDF、扫描 PDF、空文本、损坏文件分别返回 ENCRYPTED_PDF/SCANNED_PDF_UNSUPPORTED/EMPTY_MATERIAL/MATERIAL_PARSE_FAILED。异步发现错误时上传 HTTP 已返回 201，由 run.error 和 Material.failed 提供失败详情。

source_refs 统一结构为 `{material_id,material_version_id,chunk_id,page,line_start,line_end}`，不适用位置字段为 null。首版题目绑定主 topic_id；复杂题需要分题评分后分别登记证据，不能把整题分数不加区别写入多个知识点。正式测试期间 source_refs 和答案键按前文规则隐藏；用户本来已持有原资料，应用不能保证其线下不查资料，学习效果研究应说明这一限制。

### 14.4 最小端到端调用顺序

1. POST materials → 订阅 run events → 检查解析状态；需要审核时 GET graph-diff → POST publish。
2. POST learning-spaces → POST scope → POST assessments → 等待生成完成 → GET assessment。
3. POST attempts → POST finalize → 等待评分 → GET result → GET state。
4. POST plans → 等待完成 → GET plan → POST sessions → messages/练习 → finish。
5. 新资料版本：POST versions → 审核并发布图谱 → GET knowledge-updates → POST knowledge-updates/apply → 受影响主题 retest/local_replan。

验收必须覆盖：同一 finalize 重试不重复计分；评分时切换资料版本不污染新状态；生成题目校验失败不对用户发布；SSE 断线可重连；协作取消后不会补写结果；删除后检索和图谱无法恢复已删除内容；所有 API 与 Agent 工具均无任意命令执行路径。
