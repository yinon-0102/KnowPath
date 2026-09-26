# MinIO 文档存储设计

## 目标

将真实后端接收的原始资料从 MySQL 大字段迁移到 MinIO，同时保留 MySQL 中的资料元数据、内容哈希、版本信息和对象引用。新上传、异步解析读取、资料删除都走统一的原始资料存储接口。

## 架构

- Docker Compose 增加 MinIO 服务、初始化 bucket 的 mc 服务和 `keel_minio` 数据卷。
- 后端增加 `RawMaterialStore` 接口，提供 `put/get/delete`；生产环境使用 MinIO 实现，内存测试使用内存实现。
- `material_raw_files` 保留版本主键，新增 `storage_backend`、`bucket`、`object_key`、`etag`，`content` 改为可空以兼容旧数据。新记录只保存对象引用，旧记录仍可读取 MySQL 内容。
- 对象键固定为 `materials/{material_version_id}/raw`，不使用用户文件名作为路径。
- 上传在同一 SQL 工作单元中登记资料、写入对象、保存引用和入库任务；SQL 事务失败时重查已提交引用，再尝试删除未提交对象。进程强杀或补偿存储不可用的孤儿对象需要运维核对清理。
- 解析 worker 通过 `get_raw` 读取；删除流程在 SQL 行删除后由外部清理 worker 删除 MinIO 对象并支持重试。

## 配置

新增环境变量：`LEARNING_RAW_STORAGE`、`MINIO_ENDPOINT`、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`、`MINIO_BUCKET`、`MINIO_SECURE`。默认值仅用于本地 Compose，不写入代码中的生产凭据。旧部署未设置存储模式时保持 SQL；模板显式启用 MinIO。

官方社区镜像不可用且源码仓库已停止维护，因此 Compose 从固定官方源码对象构建服务端和 mc，保留许可证；基础镜像固定摘要。当前实例只监听本机，生产使用前需评估维护策略。

## 兼容与迁移

旧的 MySQL 原始内容继续可读；新增迁移命令将其上传到 MinIO 并回填对象引用，迁移成功后才允许清理旧内容。迁移过程按版本幂等，可重复执行。

## 验证

- 单元测试覆盖 MinIO 配置、对象键、put/get/delete、旧 SQL 内容回退和删除重试。
- API 测试验证上传后返回成功且异步解析仍能取得原文件。
- Compose 配置检查 MinIO 健康检查、bucket 初始化和持久卷。
