# 测评体验修复与学习路线重构

用户已批准实施，完整要求见本次会话。实施不自动重启服务，不替换模型，不改变题数和独立掌握规则，不重建排序任务，不修改稳定节点或笔记编号。

## 工作项

- [x] 修复完成带笔记渲染；区分提交失败和成功后的刷新失败；安全 Markdown 适配题干、选项、结果与复核。
- [x] 精简出题内部主题/来源标识；来源去重、公平总预算、整段供给；保留严格校验、撤销/版本/取消隔离；发布真实生成阶段。
- [x] 新计划默认原文顺序、智能可选；多资料优先级；预览及版本化幂等原子重排；固定完成/跳过/活动节点；保留日期容量、稳定身份、历史和笔记。
- [x] 紧凑横向节点卡；区分显示序号、原文章节、笔记稳定编号；真实未暂停活动会话呼吸提示，暂停立即更新；保留阅读 DOM 和滚动。
- [x] 前端 check/full test；后端测评/出题/计划/会话/笔记/资料删除回归；优化前后三次真实模型比较；浏览器桌面/窄屏/键盘/减少动态效果验收。

## 接口

创建计划新增可选 order_mode(source/adaptive) 与 material_order；initial 默认 source，local_replan 继承基础计划偏好。

POST /api/v1/plans/{plan_id}/order-preview 与 PATCH /api/v1/plans/{plan_id}/order 均包含 expected_plan_version、order_mode(source/adaptive/custom)、可选 material_order；custom 使用可移动节点 ordered_node_ids 的精确排列。预览不写入或创建 Run。应用沿用幂等键，原子递增计划版本。计划 JSON 保存顺序策略，context.position 保存可变位置，sequence/node_id 保持不变，无结构迁移。

## 验收证据

完整验收记录见 docs/implementation/2026-10-10-assessment-and-source-route-verification.md。前端 301 项、后端相关 469 项通过，14 项需要额外数据库环境的参数项跳过；真实模型耗时和浏览器模拟证据分别记录。
