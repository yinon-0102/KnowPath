# 学习工作台接口

`GET /api/v1/learning-spaces/{space_id}/workbench` 返回 `{space_id,current_plan,active_session}`。当前计划为最新非 `superseded` 计划，沿用计划接口返回字段。活动会话返回 `id,session_id,space_id,plan_id,task_id,status,started_at,paused,context`。没有计划或会话时相应字段为 `null`。创建新的初始计划会将旧计划置为 `superseded`；历史遗留的多个 `ready` 计划也只将最新一份视为可执行的当前计划。旧活动会话仍可记录事件和结束。

`GET /api/v1/learning-spaces/{space_id}/progress?limit=10&cursor=…` 按轮分页，`limit` 范围 1–50。轮按 `created_at,plan_id` 降序，游标绑定空间且不应由客户端解析。返回 `{space_id,rounds,completed_count,total_count,next_cursor}`，轮包含 `plan_id,created_at,status,nodes`，节点包含 `node_id,plan_id,task_id,sequence,title,kind,status,topic_ids,estimated_minutes,active_session_id,historical`。复制节点只在最初轮出现，详情定位到最新计划中的代表任务；同一计划优先使用非历史副本。累计计数按唯一节点统计，只有 `completed` 计入完成。

计划任务新增公开 `node_id,sequence`，并持久化在 `context`。继承任务（包括同周期仅切换复习偏好后的重新规划）、同轮延期恢复和历史副本保持身份，新学习或复习轮次生成新身份。旧任务仅在历史标记、相同周期签名或相同输入签名能够证明复制关系时合并，不根据 `origin_task_id` 单独推断新旧轮次。

`GET /api/v1/plans/{plan_id}/tasks/{task_id}/learning-record` 返回 `{space_id,plan_id,task_id,node_id,task,topics,sessions,assessments,total_elapsed_seconds,current,plan_status}`。`current` 仅对当前计划非历史任务为真。会话聚合同一稳定节点的所有任务副本；测评仅按显式任务或会话关联聚合。测评未完成时 `result=null`，已完成时复用测评结果接口的公开结果，不返回隐藏题库答案、评分规则或模型上下文。

主题基本字段为 `id,name,source_refs`，附加 `source`（`session_snapshot`、`task_snapshot`、`current`、`missing`）与 `fallback`。优先使用会话封存主题与资料定位，再用任务封存信息，缺失时补当前资料并设置 `fallback=true`。时长沿用服务端会话墙钟时间，不扣除暂停时间。缺少可计算的起止数据时 `elapsed_seconds` 为 `null`，没有有效会话时 `total_elapsed_seconds` 为 `null`；客户端应显示未记录而非填入 0。

`POST /api/v1/learning-spaces/{space_id}/assessments` 可附 `plan_id,task_id,learning_session_id`。会话单独传入时服务端补其计划与任务；传入任务须提供计划或会话。会话必须属于指定空间和确切计划任务。计划单独关联可用，但不会出现在单节点记录中。题目主题默认限于关联任务，可显式选择其子集。

已被替代计划、历史任务、跳过任务、延期任务和当前范围外任务不能创建关联测评。已完成的非历史任务支持 `kind=retest`，即使该计划因新的掌握证据暂需重新规划。关联 ID 持久化且会在测评公开信息中返回，不会送入题目生成模型；原有不带关联字段的客户端保持原行为和幂等指纹。

任务更新、局部重新规划、启动新会话与关联测评均校验当前计划。历史任务只读；当前已完成任务可以更新备注及进行复测，不能重新启动学习。任务完成、会话结束与掌握证据分别记录。
