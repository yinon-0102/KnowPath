# 当前中文资料（PDF、MD、TXT 各一份）

更新：2026-09-20。上传下列三份资料即可，不上传说明及 manifest。

| 文件 | 内容 | 说明 |
|---|---|---|
| minors-protection-law-2024.pdf | 《中华人民共和国未成年人保护法（2024年修正版）》 | 官方公开原件，简体中文，37 页，单栏文字，9 章 132 条；作为简单文本 PDF 基线 |
| linear-algebra-zh.md | 《动手学深度学习》线性代数章节 | 简体中文，保持此前选择 |
| lunyu-zh-hans.txt | 《论语》简体转换版 | 对 Project Gutenberg 繁体原文使用 OpenCC 0.1.7 的 t2s 转换；没有改写或生成正文，英文许可说明保留 |

## 来源

- PDF [官方发布页](https://cgzf.sh.gov.cn/channel_87/20241124/50ee5201540047278966e83fbcb3d4da.html)，发布单位：上海市城市管理行政执法局。
- PDF [原文件](https://cgzf.sh.gov.cn/cmsres/30/3070ca9c07c14f3fa5d1468cac0010cf/3fb1b7cd7118df6e3df3fd19a420dfb1.pdf)。测试绑定该 2024 年修正文本快照，不随在线法律版本变化自动更新。
- MD：https://github.com/d2l-ai/d2l-zh/blob/master/chapter_preliminaries/linear-algebra.md
- TXT 原文：https://www.gutenberg.org/ebooks/23839 ，下载：https://www.gutenberg.org/cache/epub/23839/pg23839.txt

TXT 保留原文件许可说明，MD 官方仓库许可证副本在上级 D2L-LICENSE。

## 使用建议和验证范围

PDF 适合原文定位、条件识别和章节综合问题，例如“本法所指未成年人的年龄范围是什么”“家庭、学校和网络保护分别规定了哪些责任”。答案关联本文件原文和物理页码；本文件没有可依赖的印刷页码。MD 用于数学概念问答，TXT 用于原句定位和篇章引用。
三份主题不同，建议分别建立学习空间。它们是候选语料，不是已经人工标注的评测集。
新 PDF 已核验：37/37 页有可提取文字，页面均约 595.3 × 841.9 pt、A4 竖版、旋转角度为 0。已检查全部页面渲染缩略图，均为单栏正文，未见图表或扫描页；图像 XObject 数为 0。
9 个章节标题可提取，条文编号第一条至第一百三十二条顺序完整，未发现 Unicode 替换字符。项目实际 MaterialParser 生成 37 个带页码的 chunk，长度为 148～623 字符。
当前解析器仍按页/空行切分，尚未自动生成章条层级；跨页条文仍可能拆开。换资料降低排版干扰，不代表分块策略已优化。本轮验证未开展真实模型回答质量评测；缩略图检查确认整体版式，并非逐字校对全文。
新 PDF 比此前约 40～50 页的偏好稍短，但全文完整，没有为凑页数重排、截取或生成内容。法规版式简单，部分法律表达仍较正式，不能单凭它概括教材、图表或全部 PDF 的效果。公开法规可能被模型记住，评测需同时检查证据召回和引用支持，增加范围排除、无答案或受控事实问题。
PDF 为简体中文，TXT 已按要求转换为简体；《论语》依然是文言文，没有变成白话注释本。
MD 保留 LaTeX、代码及教材出版标记，外链图片未下载。
manifest.json 保存实际文件的来源、大小、SHA-256 与解析统计。详细 PDF 核验记录另存于 docs/research/pdf-text-baseline/minors-verification.json。
原 44 页食物安全 PDF 有图文混排、表格和目录提取错序，保留为复杂排版样本，移至 ../previous-chinese/food-safety-guide-zh.pdf；原来源及哈希另存于该目录的 food-safety-guide-zh.manifest.json。此前 CTeX PDF 和繁体 TXT 也在该目录留档，当前目录只保留上述三份学习资料。
