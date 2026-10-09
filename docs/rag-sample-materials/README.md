# KnowPath 公开学习资料样本

获取日期：2026-09-19。文件均为发布方公开原文，未改写、未转换格式。
用途：学习资料导入、来源检索、引用定位及后续真实模型评测的候选语料；不是已经标注完成的评测集。

| 文件 | 主题/语言 | 原始来源 | 验证结果 |
|---|---|---|---|
| think-python-2e.pdf | Python 入门，英文 | https://greenteapress.com/thinkpython2/thinkpython2.pdf | 244 页，921415 字节，265 个解析片段 |
| think-stats-2e.pdf | 用 Python 做探索性统计，英文 | https://greenteapress.com/thinkstats2/thinkstats2.pdf | 264 页，2090830 字节，278 个解析片段 |
| d2l-linear-algebra-zh.md | 线性代数，中文 | https://github.com/d2l-ai/d2l-zh/blob/master/chapter_preliminaries/linear-algebra.md | 31868 字节，222 个解析片段 |
| d2l-calculus-zh.md | 微积分，中文 | https://github.com/d2l-ai/d2l-zh/blob/master/chapter_preliminaries/calculus.md | 12937 字节，73 个解析片段 |
| rfc8259-json.txt | JSON 数据交换规范，英文 | https://www.rfc-editor.org/rfc/rfc8259.txt | 28360 字节，205 个解析片段 |

## 如何使用

建议分别创建“Python 基础”“数学与统计”“JSON 规范”学习空间，避免首轮评测混入无关来源。
先用两篇中文 Markdown 和较短的 JSON 规范建立小规模基线，再导入整本 PDF 测试长文档检索和页码引用。
上传表中五份文件即可，README、manifest、许可证和辅助脚本不属于学习材料。

候选提问（尚未人工标注标准答案与证据，不应直接作为已完成测试）：
- Python：列表和元组有什么区别？函数参数与局部变量的作用域是什么？
- 统计：PMF 和 CDF 有什么区别？为什么相关性不能单独证明因果关系？
- 线性代数：矩阵乘法和按元素乘法有什么区别？向量范数表达什么？
- 微积分：梯度与方向变化有什么关系？链式法则如何用于复合函数？
- JSON：对象成员名称是否必须唯一？重复名称可能导致什么互操作性问题？NaN 是否是合法 JSON 数值？
- 无答案对照：在只绑定 JSON 规范的空间里询问“Python 列表如何排序”，检查是否说明资料不足。
- 追问：先问“列表与元组的区别”，再问“后者能修改吗”，检查上下文指代。

## 已知解析限制

五份文档均通过当前 MaterialParser；均低于 20 MiB，PDF 均低于 300 页。解析片段均未超过 6000 字符。
两本 PDF 分别有 243/244、263/264 页能提取非空文本；空白页不等于扫描件。
PDF 检查期间出现缺少 fontTools 的字体解析警告及图形 XObject 调用上限警告：正文可提取不代表公式、图表和特殊符号完整，不应把依赖这些内容的问题当作首批质量基准。未逐页视觉验收。
中文 Markdown 是教材发布源文件，保留 LaTeX、代码、多框架重复示例和出版指令；引用图片没有随文件下载。当前解析器不渲染公式、图片或教材专用标记。
这是格式多样的语料集合，不是同一篇文章的三种格式；不能直接据此比较不同格式的准确率。公开教材也可能已被模型预训练覆盖，评测必须核验引用及范围，不能只看答案正确性。

## 来源与许可

- Think Python：PDF 版权页声明 CC BY-NC 3.0。
- Think Stats：PDF 版权页声明 CC BY-NC-SA 4.0。
- d2l-zh：下载时官方仓库 LICENSE 为 Apache 2.0，副本保存在 D2L-LICENSE；另保留原文中的版权/署名信息。
- RFC 8259：保留文档自身 Copyright Notice、BCP 78 及 IETF Trust 条款。

两本教材含非商业许可限制；若后续用于商业产品分发，需要另行核对授权。
manifest.json 保存每个文件的实际来源 URL、大小、SHA-256 和解析统计，可固定这次语料版本。
