# 学习空间双模式实施记录

依据已确认方案：学习空间直接进入学习区，顶部当前轮次节点进度，滚动后缩为吸顶条；原文阅读与个性化讲解在同一区域切换。沿用现有配色和真实数据。

## 实施清单

- [x] 前端独立学习控制器与阅读视图；节点、来源、讲义请求隔离。
- [x] 原 PDF 按冻结版本鉴权读取，原始文本保留原内容；文件缺失时显式选择解析片段。
- [x] 讲义预设、补充要求、按需生成、版本阅读、取消和重试入口。
- [x] 会话显式开始、暂停和保存；关联测评、笔记与学习记录入口。
- [x] 原文与讲义重新展示前审计；异步读取失败不展示保留正文。
- [x] 后端讲义记录、内存实现、0017 迁移、Run 与 Outbox worker 接入。
- [x] 节点讲义 API、追问来源约束及审计会话历史接口。
- [x] 后端专项、迁移与相关回归实际运行通过。
- [x] 本地 MySQL 应用 0017 迁移。
- [x] 真实模型生成示例讲义，重新加载确认数据库恢复。
- [ ] 真实浏览器和真实模型完整流程验收。

## 接口

所有接口沿用 `/api/v1` 及本地鉴权。

| 方法与路径 | 用途 |
| --- | --- |
| GET/POST `/learning-spaces/{space_id}/nodes/{node_id}/lessons` | 讲义元数据列表 / 显式生成 |
| GET `/study-lessons/{lesson_id}` | 审计后读取讲义正文 |
| POST `/study-lessons/{lesson_id}/retry` | 创建新 Run 重试原版本 |
| POST `/learning-spaces/{space_id}/messages` | 可选 `lesson_id` 限定节点追问 |
| GET `/learning-spaces/{space_id}/conversations/{session_id}/messages` | 分页读取并审计已保存问答 |

讲义风格：`plain`、`examples_first`、`concise`、`deep`。空间偏好扩展 `lesson_style`、`lesson_instructions`，保留原偏好字段。讲义与测评后笔记分别保存，阅读不改变任务完成或掌握状态。

## 验证记录

前端 `npm.cmd run check` 已通过；`npm.cmd test` 全部 228 项通过，学习区专项 24 项。

后端相关回归 259 项通过、34 项跳过。审批服务恢复后已实际运行测试，并升级真实 MySQL 至 `0017_study_lessons (head)`。现有 Python 体验空间真实模型讲义生成成功，四个正文区块与四个引用来源入库，重新加载一致。最初 Python 沙箱与审批 HTTP 503 阻塞已经解除。

真实浏览器完整流程尚未验收，验证记录与人工检查步骤见 `docs/implementation/2026-10-09-learning-space-dual-mode.md`。

本期不调整全局首页、计划排序、不自动批量生成历史讲义、不启动常驻服务。工作基于当前 `codex/backend-integrity-repairs` 分支已有改动，未创建丢失未提交功能的独立干净副本。
