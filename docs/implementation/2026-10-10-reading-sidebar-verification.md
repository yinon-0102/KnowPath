# 阅读侧栏实施与验收

日期：2026-10-10。沿用现有蓝灰配色。未迁移数据库，未新增服务，未重启项目服务。

## 实现

后端消息契约新增互斥的 reading_context；空间绑定版本在请求、worker、正文交付与最终发布时验证。原文使用配置的来源限定检索/生成链，允许检索节点外但仍在当前文档和空间范围内的 parsed chunks；缺少向量索引明确失败。原文记忆、讲义记忆与普通问答隔离。历史返回 reading_context 与 lesson_id，来源删除擦除依赖快照并清空可重放正文。

前端按空间、节点和阅读依据隔离会话、草稿、待接收 Run；刷新重新读取并审计正文，不在本地保存模型回答。切换中止订阅，保留任务；停止则取消 Run。订阅清理与提交清理分别校验所有权，历史分页及迟到提交带失效代次隔离。SSE 保留后端错误码，使失效范围立即清除旧正文。

桌面 340px 默认展开侧栏；手机抽屉支持焦点限制、Escape、背景 inert。下方追问移入侧栏。来源跳转、生成状态、停止与恢复仍可用。白底选中节点、蓝标题短线、单层学习中圆点和完成勾号保留无障碍状态。

## 自动回归

- `npm.cmd --prefix frontend run check`：退出码 0。
- `npm.cmd --prefix frontend test`：261 passed，0 failed。
- 后端 23 个相关文件：393 passed，43 skipped，1 warning，82.50 秒。涵盖 reading/messages/provider/retrieval、document_scope、RAG、lessons/streaming、material/model-task/space deletion、assistance/review audit、runs。43 个跳过项需要显式专用 MySQL/Neo4j 配置或 MySQL 锁调度，不计为通过。
- `git diff --check`：退出码 0，仅换行格式提示。

完整日志：`.runtime.local/sidebar-frontend-tests.log`、`.runtime.local/sidebar-backend-tests.log`。后端使用现有 venv pytest，临时数据目录为 `py/.tmp/reading-root-final`。

## 浏览器验证

独立临时无界面 Edge，实际前端端口 5173，1440px 与 390px：

| 要求 | 观察证据 |
|---|---|
| 桌面默认侧栏 | 340px，role=complementary，默认展开 |
| 手机抽屉 | 默认隐藏；打开 role=dialog、aria-modal=true；焦点在关闭按钮；Escape 后无残留 inert |
| 布局 | 1440px、390px 均无横向溢出 |
| 阅读和输入保留 | 收起再展开仍为同一阅读 DOM，Markdown scrollTop=180，未提交输入完整 |
| 讲义模式切换 | 受控临时讲义：原文/讲义切换后 scrollTop 180→180 |
| PDF 不重载 | 受控 PDF 与 SSE：同 iframe、同 contentWindow，流式更新 load 事件数 0 |
| SSE 增量 | 实际页面侧栏显示受控 message.delta，最终事件清除草稿 |
| 运行异常 | 观察到 0 个前端 Runtime 异常 |

受控讲义、PDF 和 SSE 响应只存在于临时浏览器 fetch，不写入业务数据库，不调用真实模型或创建测评/学习任务。真实原文读取和权限访问使用既有样例空间。截图 `.runtime.local/sidebar-desktop.png`、`sidebar-mobile.png`，脚本 `.runtime.local/sidebar-browser.mjs`。

## 手动生效

手动重启 API 和模型 worker 以加载后端源码，刷新前端页面。前端开发服务仍使用现有进程，静态源码刷新即可；若浏览器有旧资源缓存可强制刷新。无需数据库迁移。
