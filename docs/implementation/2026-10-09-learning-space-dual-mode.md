# 学习空间双模式：实现与验收

## 当前实现

空间默认进入 `#/space/{space_id}/study`。顶部节点切换任务，完整进度滚动后缩成吸顶条，历史轮次折叠查看。旧 `overview` 及会话入口跳转到新学习区。

原文模式读取空间绑定版本的原文件，PDF 内嵌阅读并定位页码，文字保留原始内容。原文件不可用时提供明确标识的解析文本片段。模型讲义采用按需生成，四个正文分区带原文引用，保存版本并提供取消和重试。讲义偏好按空间保存。测评后的笔记继续使用独立自动总结流程。

来源、讲义与问答历史读取接入辅助作答审计。前端重新切换阅读模式时先核验，核验中隐藏缓存正文；失败后清除阅读内容。计时只在显式开始会话后记录，保存不会自动标记任务完成或已掌握。

## 已执行检查

- 前端专项测试覆盖真实来源版本、显式生成、偏好恢复、页码恢复、延迟响应隔离、缓存正文核验、会话与问答生命周期、刷新幂等请求，以及节点笔记入口。
- 前端 `npm.cmd run check` 通过；`npm.cmd test` 全部 228 项通过，其中学习区专项 24 项。
- 后端讲义、会话、消息、worker、笔记生命周期、资料删除、资料范围、上下文、检索及迁移相关回归：259 项通过、34 项跳过；一条现存 Starlette 弃用警告。跳过项包括需要额外外部环境的测试。
- 最终落盘后再次独立执行讲义、计划会话、消息、worker、删除、笔记生命周期、迁移与资料范围 API 等十个模块：228 项通过、18 项跳过。
- 独立审查发现的旧 Run 事件污染、重启恢复、重试幂等、归档写保护、讲义连续追问和资料范围泄漏均已修复并增加回归测试。
- 本地真实 MySQL 已从 `0016_learning_notes` 升级至 `0017_study_lessons (head)`。
- 使用现有 `示例｜Python 入门体验` 空间真实模型生成一份“通俗易懂”讲义：4 个正文分区、4 个原文来源，状态 `completed`；重新创建后端状态后读取正文一致，确认 MySQL 恢复。记录位于 `.runtime.local/study-space-smoke.json`。未创建会话、成绩或掌握状态。

## 尚未执行

浏览器内嵌 PDF 的实际显示、响应式布局及“学习→测评→笔记”的真实浏览器完整流程尚未验收。后端测试、MySQL 迁移和真实模型讲义验证已完成；不可把自动化测试或直接后端调用等同于浏览器验收。

需要重新验证时可执行：

```powershell
cd C:\Users\27202\Desktop\KnowPath\py
.\.venv\Scripts\python.exe -m pytest knowpath_backend/test/test_study_lessons.py knowpath_backend/test/test_learning_plans_sessions.py knowpath_backend/test/test_learning_messages.py knowpath_backend/test/test_learning_model_tasks.py knowpath_backend/test/test_learning_notes_lifecycle.py knowpath_backend/test/test_learning_migrations.py --basetemp="C:\Users\27202\Desktop\KnowPath\.runtime.local\pytest-study-space-manual" --tb=short -q
```

本机迁移已应用；其他数据库需先应用新增 `0017_study_lessons` 迁移。API、现有模型 worker 和前端仍由用户手动启动或重启：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

当前没有启动或重启任何常驻服务，没有创建伪造学习进度或测评结果。

### 后续启动故障修复

实测正在运行的 5173 服务能返回最新 `app.js`，但 `/src/study.js` 返回 404，原因是旧前端进程仍持有新增模块之前的白名单。页面此前因静态导入失败停留在初始文案。现已增加可捕获模块加载错误的启动过程和重启/刷新提示，静态服务测试覆盖完整动态依赖。新增回归后前端完整测试 230 项通过，语法检查通过。旧进程仍需由用户手动重启一次；本次没有替用户启动或重启服务。

## 人工完整流程

1. 打开现有 Python 体验空间或法律 PDF 学习空间，确认顶部节点与当前计划一致。
2. 选中节点，检查原 PDF 及相关页码；切换风格生成讲义，核对每节引用。
3. 刷新页面确认讲义可恢复，改变风格生成新版本，旧版本可读。
4. 显式开始学习，暂停、继续，围绕讲义追问后刷新确认历史恢复。
5. 结束并测评，确认测评关联当前任务，结果保存在节点，笔记使用同一节点章节。
6. 验证失败重试、活动测评读原文/讲义的辅助标记、资料更新与删除、空间归档及删除。
