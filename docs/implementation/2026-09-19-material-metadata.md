# 资料元数据更新

PATCH /materials/{material_id} 严格接受 name（1–100 字）和 status=archived；expected_version 必填，不接受空更新、未知字段或强制状态跳转。

Material.version 从 1 开始，在资源锁内检查并递增。内容上传创建新版本也递增该资源版本，幂等重放不递增。改名与归档通过仓储落库；归档后禁止新空间绑定，现有空间、版本、题目与证据保留。

0007_material_version 为已有资料回填 version=1，Material DTO 暴露该值供后续 PATCH/DELETE 使用，不与内容版本 current_version_id 混用。MySQL 两个并发更新请求只有一个成功，另一个返回 VERSION_CONFLICT。定向测试覆盖 memory/SQLite/MySQL、HTTP 严格验证、迁移升级与降级保留旧数据。
