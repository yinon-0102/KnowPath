# 真实资料评测草稿：全部待人工审核

本目录包含三份真实资料各20题，共60题。问题及答案要点由代理起草，**没有人工审核通过记录，也没有运行留出集**。40题属于开发集，20题属于留出集；按主题族划分，同族题目不跨分区。

`dataset.jsonl` 保留现有评测 schema；`split.json` 为固定主题族分区；`source-manifest.json` 保存原文件哈希及旧解析块清单；`validation-report.json` 只报告机械校验结果。

## 来源与坐标

`sources/<原文件SHA256>-<旧块序号>.txt` 是现有 MaterialParser 提取的不可变正文，按UTF-8保存，不增补标点或清洗正文。引用的 start/end 是该文本的Unicode字符半开区间。artifact_hash 校验提取文本，raw_source_sha256 校验原始PDF/MD/TXT文件；二者不可混用。原解析器已移除的Markdown标题或空行不是证据。

每个引用包含 source_filename、raw_source_sha256、从0开始的 source_ordinal、page、block 和完整引用文本。material/version/block/scope ID 是明确标识的确定性草稿占位值。接入实际评测数据库前，必须以原文件哈希、旧块序号及正文哈希三者共同核验，映射到真实版本/旧块，重算 scope snapshot；不得仅凭偏移或文件名匹配。

允许范围为题目对应的完整资料。necessary_evidence 是支持答案要点的原文语义单元；部分单元包含比最小证据更长的上下文，人工审核时应决定是否裁成更精确的必要跨度。context_evidence 为不可答/需澄清题提供真实背景，不冒充支持缺失事实的gold。部分回答题明确列出缺失信息。

## 审核步骤

1. 按原文件及来源片段核对每题的提问、答案要点、条件与例外；检查不同版本字词，不用外部知识替换原文。
2. 核对必要证据是否足够且最小，是否覆盖所有要点；尤其检查跨页法条、公式和古文解释。
3. 检查不可答题是否真的缺少所需对象或事实，澄清题是否确有多种合理指代；不把局部检索未命中当作全文缺失。
4. 复核主题族独立性与40/20分区。禁止根据留出集执行结果调整问题、提示词或检索参数。
5. 由真实审核者记录姓名/标识、时间、判断与修改理由后，才可逐题批准并更新split及冻结配置的审核状态。不要批量伪造approved。
6. 实际留出运行仍需遵守已有harness的人工批准、冻结价格和数值成本/延迟门槛；本草稿不自动设置这些条件。

重新生成（只读取本地原材料，不调用模型或重排服务）：

```text
python -m knowpath_backend.rag_eval.prepare_real_dataset --sample-dir <原材料目录> --output <本目录>
python -m knowpath_backend.rag_eval.prepare_real_dataset --check --output <本目录>
```

生成器会拒绝覆盖内容已发生变化的同名来源文件。重新生成会覆盖问题草稿；人工修改后应保存审核版本，勿再无检查重跑生成命令。
