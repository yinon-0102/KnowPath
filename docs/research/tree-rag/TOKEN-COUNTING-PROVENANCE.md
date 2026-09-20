# 请求计数、模型绑定与容量预留（2026-09-21）

实现入口：`py/knowpath_backend/learning/rag/token_budget.py`。本说明不表示已找到与
DashScope `qwen-plus` 部署别名完全一致的公开 tokenizer，也不表示模型质量已验收。

## 已核对的官方资料与限制

读取了 [Qwen 官方概念说明](https://qwen.readthedocs.io/en/latest/getting_started/concepts.html)。
页面说明 Qwen 使用 BPE、控制 token 和 ChatML；这可以解释为什么必须包含聊天模板，
不能证明任意开源 Qwen 权重的 tokenizer 与商业 `qwen-plus` 别名相同。
2026-09-21 读取的 HTML 文本以 UTF-8 编码后 SHA256 为
`69aaf1072de0e1b01d64e96e1b796060f6b60f79743d80c2fecd0b2217a9c814`。
这是资料页面摘要，**不是 tokenizer 文件摘要**，该 latest 页面本身也不是不可变版本。

尝试读取的阿里云 `token-calculation`、`developer-reference/tokenizer` 路径返回了错误页或页面外壳；
官方 DashScope Python SDK 的 GitHub tree API 本次受到匿名速率限制。
因此本次没有验证到可供该部署别名直接使用的官方计数接口或不可变 tokenizer artifact。
没有调用付费推理接口，没有安装或下载任意 Qwen tokenizer 冒充已匹配版本。

## 默认保守 profile

`ConservativeByteProfile(provider, model)` 将完整紧凑 JSON 请求的 UTF-8 字节数，加上
每条消息 32、每个请求 64 的模板余量，作为请求准入值。模型名称必须精确匹配。
系统提示、问题、证据、响应格式及其他请求参数均计入 envelope。

- `kind=conservative_upper_bound`，`unit=utf8_bytes_plus_token_reserve`；
- `estimated_tokens=None`，`upper_bound_tokens=value`；
- `profile_id` 包含版本、provider、部署 model 和两项余量；
- 实際供应商 `prompt_tokens`/`completion_tokens` 独立记录，缺失时保持 unknown；
- `calibrate_usage` 可报告实际输入是否超过该准入上界，不自动更改配置或据此推算失败账单。

这是以 byte-level tokenization 和配置模板余量为条件的**保守操作上界**，
不是对未公开服务端模板的数学保证。32/64 是明确的配置余量，不是已测得的 `qwen-plus` 模板开销。
实际 usage 若超过它，应判为 profile 失配并停止把该 profile 当作可靠准入依据。
没有使用 bytes/3，也没有把 OpenAI 的 tiktoken 当作 Qwen 的精确计数器。

## 可选、需要先审核的本地 tokenizer

`load_verified_local_profile(path, expected_manifest_sha256=..., provider=..., model=...)`
只接受与调用方提供的 SHA256 相符的本地 manifest。它要求以下内容：

| 字段 | 约束 |
|---|---|
| `version` | 整数 `1` |
| `provider`, `model` | 与实际部署配置逐字一致 |
| `model_revision` | 经审核的部署版本，不应只填可变别名 |
| `artifact_revision` | 不可变源码或模型 artifact commit，40–64 位小写十六进制 |
| `source_url` | artifact 的 HTTPS 来源 |
| `deployment_match_evidence` | 证明此 artifact 对应该部署的供应商文档或已审核证据引用 |
| `files` | 专用目录下全部 artifact 的相对路径到 SHA256 映射；至少含 tokenizer.json 和 tokenizer_config.json |
| `chat_template_sha256` | tokenizer_config.json 中 chat_template 字符串 UTF-8 编码的 SHA256 |
| `chat_template_kwargs` | 当前仅接受 `{"enable_thinking": false}` |
| `safety_margin_tokens` | 正整数余量 |

manifest 放在只包含该 tokenizer 文件的专用目录。路径越界、摘要不符、未列出的文件、
模型/provider 不符、缺少来源或部署匹配证据均拒绝。manifest 摘要应来自独立审核配置；
由未经审核的 artifact 临时生成摘要，不构成模型匹配证明。

可选的 `transformers` 依赖未加入默认运行环境。加载固定为
`local_files_only=True, trust_remote_code=False, use_fast=True`，不下载模型、不执行远程代码。
加载后再次检查模板一致性；计数使用 `apply_chat_template(tokenize=True,
add_generation_prompt=True, enable_thinking=False)`，另外预留请求 envelope 的 UTF-8 字节数和配置余量。
只支持已审核的纯文本、不思考、非流式、JSON object 请求；工具、多模态、自定义 schema 等额外能力需另行审核，
不能沿用这个计数 profile。

本地结果仍标为 `tokenizer_estimate`，`estimated_tokens` 是本地聊天模板的 token 数，
`value` 含准入余量，`upper_bound_tokens=None`。服务端额外包装可能不同；本地计数不是账单实际值。
文件摘要验证的是完整性，`deployment_match_evidence` 是审核声明，代码本身不能证明供应商部署映射。
当前没有提供适用于 `qwen-plus` 的已审核 manifest，也没有声称本地 tokenizer 验证已完成。

## 所有阶段共用的完整协议预算

`ModelBudget(application_input_limit, context_window_tokens, output_limit_tokens)` 分别验证应用输入限额、
模型上下文窗口及输出预留；金额和物理调用次数由编排层独立控制。

`pack_evidence_groups(rows, build_requests=..., profile=..., budgets=...)` 在选取证据前先检查空证据的固定协议。
随后每次尝试加入证据，都使用调用方提供的完整生成、核验、修正请求逐阶段计数。
`StageRequest(stage, body, reserved_input_tokens)` 为尚未产生的草稿/反馈预留容量。
阶段集合必须完整，阶段输出 `max_tokens` 必须与预算一致。

未来输入预留必须覆盖完整序列化结构、JSON 转义和重复引用带来的扩展。
不能直接把上一阶段的 output token 上限当作其 UTF-8 字节上界；调用方应通过有界协议结构或已审核的
tokenizer 最大 token 字节长度确定预留，再在实际请求发送前重新检查。

证据的 `requires` 传递依赖和 `evidence_group` 同组成员作为整体保留，不裁剪原文条件。
返回选择结果、未选 ID、缺依赖 ID 和逐阶段计数；固定协议超限或所有合法完整证据组都放不下时，
抛出带明确原因的 `TokenBudgetExceeded`。缺依赖与容量不足分别表示，不能当作“资料不存在”。

离线测试覆盖保守单位标注、envelope、usage unknown、模型绑定、所有阶段预留、整组依赖、
固定协议超限及本地 manifest/文件/模板不匹配。使用测试 tokenizer stub 验证加载调用协议，
不将 stub 的通过结果当作真实 tokenizer 或实际模型的验收。
