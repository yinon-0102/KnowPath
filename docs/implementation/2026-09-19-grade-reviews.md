# 评分复核

按 API 文档实现 POST /assessments/{assessment_id}/grade-reviews。复用封存题目、评分标准与原 attempt；开放题无法可靠评分时保持 unverified。reason 作为审计说明，不影响评分。

原证据标记撤销，替代证据保留原作答时间、题目族、提交序号、epoch、revision 和不可变 observation_id；复核不会新增独立作答或改变窗口排序。复核、证据撤销、掌握状态重算、测验结果、Run 和幂等响应共享事务。已 reset 或过期版本的证据不会恢复有效性。

覆盖三种仓储重启/幂等、并发、回滚、辅助作答、未答、连续复核、20 条窗口边界及 HTTP 严格请求校验。无需新增数据库表，审计记录使用 assessment context 与 evidence payload 持久化。
