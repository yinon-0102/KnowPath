# 学习工作台实施计划

已获用户授权：完成三项顶栏、五项空间分区、空间累计及当前计划节点进度条、节点学习详情、服务端恢复、测评与任务关联。沿用原有 CSS 变量、字体、卡片、按钮及技术栈。

## 分工与接口约定

- 后端：workbench / progress / learning-record 只读接口；任务稳定 node_id 和 sequence；测评可选 plan_id/task_id/learning_session_id 并校验空间归属。
- 进度组件：独立 progress.js，接收接口数据，输出现有风格的 HTML；测试节点状态、乱序、详情、空记录与历史。
- 主页面：路由、空间外壳、分区、API 客户端、懒加载与恢复、抽屉、任务/会话/测评跳转、偏好设置、准备及更新提示。

## 公共返回格式

workbench: {space_id, current_plan: existing plan or null, active_session: {id,session_id,space_id,plan_id,task_id,status,started_at,paused,context} or null}。

progress: {space_id, rounds:[{plan_id,created_at,status,nodes:[{node_id,plan_id,task_id,sequence,title,kind,status,topic_ids,estimated_minutes,active_session_id,historical}]}], completed_count,total_count,next_cursor}。limit 默认 10 轮，上限 50，cursor 不透明；当前轮最新，历史按时间降序。复制的节点仅在最早创建轮展示；节点的 plan_id/task_id 指向可查详情的最新代表，当前轮接口节点可通过 current_plan.tasks 显示相同 node_id。完成计数针对唯一节点，跳过/延期不算完成。

learning-record: {space_id,plan_id,task_id,node_id,task,topics:[{id,name,source_refs}],sessions:[{session_id,plan_id,task_id,status,started_at,finished_at,elapsed_seconds,paused,context}],assessments:[{assessment_id,kind,status,created_at,learning_session_id,result}],total_elapsed_seconds}。只聚合同一 node_id 的任务副本；测评 result 复用安全公开结果，未完成 result=null。源资料和主题优先使用会话封存上下文，缺失用当前资料补充并标注。旧记录不猜测关联。

## 页面与状态

全局：学习首页 / 学习空间 / 资料库。空间：overview首页、plan学习计划、materials资料与范围、assistant学习助手、assessment测评、review复习；后两项合为“测评与复习”主分区子页。settings/analysis 为次级页面。规范 URL #/space/{id}/{section}，子页可附 query；兼容旧链接。

节点：completed 蓝底勾，pending灰空心，active蓝描边，skipped/deferred灰色加标记。相邻节点均完成才给连接线着色。节点按钮 data-action=progress-node data-plan data-task；详情关闭 data-action=close-record，跳转用既有 #/manage/session/plan/task、#/manage/task/plan/task、#/manage/assessment-result/id；节点抽屉由主页面管理。

## 验证

先写并运行失败测试，再实现。前端 npm run check / npm test；后端新接口、继承节点、多次会话、空间隔离、测评关联及现有计划/测评回归，覆盖 memory/SQLite，可用时运行 MySQL。浏览器核对真实交互与窄屏、键盘关闭/返回焦点。全部功能通过证据审计后才标记目标完成。
