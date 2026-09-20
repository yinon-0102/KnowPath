"""Prepare review-pending real-material evaluation drafts; never run providers."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from knowpath_backend.learning.materials.service import MaterialParser, MaterialVersion
from knowpath_backend.learning.rag.chunking import semantic_chunks
from .dataset import _source_paths, _validate_split

SAMPLES = {
    "law": "minors-protection-law-2024.pdf",
    "math": "linear-algebra-zh.md",
    "lunyu": "lunyu-zh-hans.txt",
}


def draft(family, question, facts=(), *, status="answered", context=(), missing=(), history=(), categories=()):
    return dict(family=family, question=question, facts=facts, status=status, context=context,
                missing=missing, history=history, categories=categories)


# These are hand-authored draft questions and answer-point interpretations. All
# source quotations are obtained by matching actual parsed text at build time.
# @ selects the complete semantic unit containing a unique literal anchor.
SPECS = {
"law": [
    draft("law_general", "这份《未成年人保护法》如何界定未成年人？", [("@第二条", "未成年人是未满十八周岁的公民。")]),
    draft("law_general", "处理涉及未成年人的事项应坚持什么原则，并满足哪些保护要求？", [("@第四条", "坚持最有利于未成年人的原则；给予特殊、优先保护，尊重人格尊严，保护隐私和个人信息，适应身心发展，听取意见，保护与教育相结合。")]),
    draft("law_general", "发现未成年人受到侵害时，普通个人的权利与密切接触未成年人单位工作人员的报告义务有什么区别？", [("@第十一条", "任何组织或个人有权劝阻、制止、检举或控告；有关单位工作人员在工作中发现受到、疑似受到侵害或面临危险，应立即向公安、民政、教育等部门报告。")], categories=("condition_exception",)),
    draft("law_family", "第二十一条对无人看护和脱离监护单独生活分别设置了什么年龄条件？", [("@第二十一条", "未满八周岁或因身心原因需特别照顾的未成年人不得处于无人看护状态，也不得交不适宜人员临时照护；未满十六周岁不得脱离监护单独生活。")]),
    draft("law_family", "父母因外出务工委托照护时，被委托人需要什么条件，哪些人不得受托？", [("@第二十二条", "应委托有照护能力的完全民事行为能力人，综合考察并听取有表达能力未成年人的意见；有规定的侵害犯罪、吸毒酗酒赌博恶习、拒不或长期怠于履行照护义务等情形者不得受托。无正当理由不得委托他人。")]),
    draft("law_family", "监护人决定与孩子权益相关的事项之前，法律要求如何考虑孩子的意见？", [("@第十九条", "应根据年龄和智力发展状况，事前听取未成年人意见，充分考虑其真实意愿。")]),
    draft("law_school", "学校和幼儿园教职员工能否以教育为由实施体罚或变相体罚？", [("@第二十七条", "应尊重未成年人人格尊严，不得实施体罚、变相体罚或其他侮辱人格尊严的行为。")]),
    draft("law_school", "对尚未完成义务教育的辍学未成年学生，学校应如何处理；劝返无效后怎么办？", [("@第二十八条", "学校应登记并劝返复学；劝返无效应及时向教育行政部门书面报告，不得违规开除或变相开除。")]),
    draft("law_school", "学校发现学生欺凌后应立即采取哪些措施？严重欺凌能否只在校内处理？", [("@第三十九条", "立即制止并通知双方监护人参与认定处理，提供心理辅导、教育引导及必要家庭教育指导；严重欺凌不得隐瞒，应及时报告公安和教育行政部门并配合处理。")]),
    draft("law_school", "严重学生欺凌应报告哪些部门？请再给出我所在学校辖区派出所的联系电话。", [("@第三十九条", "严重欺凌应及时向公安机关、教育行政部门报告。")], status="partial", missing=("未提供用户所在学校、辖区及派出所联系方式，不能据此给出电话号码。",)),
    draft("law_social", "游艺娱乐场所的电子游戏设备可以在什么例外时段向未成年人提供？年龄难以判断时经营者怎么办？", [("@第五十八条", "除国家法定节假日外不得向未成年人提供相关电子游戏设备；经营者应设置禁入限入标志，难以判明年龄时要求出示身份证件。不能把设备的节假日例外扩展成所有场所准入许可。")], categories=("condition_exception",)),
    draft("law_social", "密切接触未成年人单位的违法犯罪记录查询，是只在招聘时查询一次吗？", [("@第六十二条", "招聘时查询规定的侵害等违法犯罪记录，有记录者不得录用；还应每年定期查询工作人员，发现相关行为应及时解聘。")]),
    draft("law_social", "请列出我家附近违反第五十八条的酒吧名称和详细地址。", status="insufficient", context=("@第五十八条",), missing=("资料和问题未提供用户地址、具体经营场所或违法调查事实。",)),
    draft("law_social", "这种情况需要立即报告吗？", status="clarify", context=("@第十一条", "@第六十二条"), history=("我在了解工作中发现疑似侵害，以及招聘时查询犯罪记录这两种情形。",), missing=("需要澄清‘这种情况’指疑似侵害还是招聘查询。",)),
    draft("law_internet", "通过网络处理不满十四周岁未成年人的个人信息需要谁同意，有没有法律例外？", [("@第七十二条", "遵循合法、正当、必要原则；不满十四周岁信息处理应征得父母或其他监护人同意，法律、行政法规另有规定除外。")], categories=("condition_exception",)),
    draft("law_internet", "仅依据所给法律文本，网络游戏服务对未成年人有哪些实名及夜间服务要求？", [("@第七十五条", "要求以真实身份信息注册和登录；不得在每日二十二时至次日八时向未成年人提供网络游戏服务。不要从外部补入其他规则。")]),
    draft("law_internet", "面向未成年人的在线教育产品能否插入游戏链接或推送与教学无关的广告？", [("@第七十四条", "不得插入网络游戏链接，不得推送广告等与教学无关的信息。")]),
    draft("law_internet", "网络产品服务提供者应怎样设置涉未成年人投诉渠道？请同时给出某款未具名应用的具体投诉网址。", [("@第七十八条", "应建立便捷、合理、有效的投诉举报渠道，公开方式等信息，并及时受理处理涉未成年人投诉举报。")], status="partial", missing=("未说明应用名称，也未提供其具体投诉网址，不能编造。",)),
    draft("law_judicial", "案件中未成年人的身份信息是否一律不得披露？请说明信息范围和例外。", [("@第一百零三条", "不得披露姓名、影像、住所、就读学校及其他可识别身份信息，但查找失踪、被拐卖未成年人等情形除外。")], categories=("condition_exception",)),
    draft("law_judicial", "未成年人权益受侵害但相关人员未代为起诉时，检察院可以做什么？涉及公共利益时呢？", [("@第一百零六条", "检察院可以督促、支持起诉；涉及公共利益时有权提起公益诉讼。")]),
],
"math": [
    draft("math_scalar", "本节中的标量是什么，在张量中如何表示？", [("@仅包含一个数值被称为", "标量仅包含一个数值。"), ("@标量由只有一个元素的张量表示", "标量可以由只有一个元素的张量表示。")]),
    draft("math_scalar", "本节的实数符号和‘属于’符号分别表示什么？", [("@本书用$\\mathbb{R}$表示", "R 表示所有连续实数标量的空间，属于符号表示是集合中的成员。")]),
    draft("math_vector", "向量的元素或分量是什么？本节用贷款样本举了哪些分量的例子？", [("@向量可以被视为标量值组成的列表", "向量是标量值的列表，标量值是元素或分量；贷款例子包括收入、工作年限和过往违约次数。")]),
    draft("math_vector", "为什么‘向量的维度’与‘张量的维度’可能指不同的东西？", [("@然而，张量的维度用来表示张量具有的轴数", "向量或轴的维度指元素数量即长度；张量维度指轴数。")]),
    draft("math_vector", "一维张量表示向量时，shape 如何表达长度？shape 含几个元素？", [("@形状（shape）是一个元素组", "shape 列出各轴长度；只有一个轴的向量张量的 shape 只有一个元素。")]),
    draft("math_matrix", "矩阵 A 属于 m×n 的实数空间时，m、n 和 a_ij 分别表示什么？", [("@其由$m$行和$n$列的实值标量组成", "m 是行数，n 是列数，a_ij 是第 i 行第 j 列的元素。")]),
    draft("math_matrix", "矩阵转置如何改变元素位置和形状？", [("@当我们交换矩阵的行和列时", "转置交换行列，b_ij=a_ji；m×n 矩阵转置为 n×m。")]),
    draft("math_matrix", "本节如何定义对称矩阵？", [("@作为方阵的一种特殊类型", "对称矩阵是等于自身转置的方阵。")]),
    draft("math_elementwise", "两个相同形状张量做按元素二元运算后，结果形状会改变吗？", [("@任何按元素二元运算的结果都将是相同形状的张量", "相同形状的两个张量进行按元素二元运算，结果保持相同形状。")]),
    draft("math_elementwise", "张量乘以或加上标量时，会怎样作用于元素和形状？", [("@将张量乘以或加上一个标量不会改变张量的形状", "形状不变，每个元素分别与该标量相乘或相加。")]),
    draft("math_reduction", "对矩阵 sum 指定 axis=0 与 axis=1 时，分别消去哪一条轴？", [("@由于输入矩阵沿0轴降维以生成输出向量", "axis=0 汇总所有行，消去轴0。"), ("@指定`axis=1`将通过汇总所有列的元素降维", "axis=1 汇总所有列，消去轴1。")]),
    draft("math_reduction", "cumsum 沿指定轴计算什么，会降维吗？在我的电脑上处理一百万个元素需要多少毫秒？", [("@此函数不会沿任何轴降低输入张量的维度", "cumsum 沿指定轴求累积总和，不沿任何轴降低维度。")], status="partial", missing=("没有用户电脑、数据类型、库版本及实测条件，资料不能提供具体运行毫秒数。",)),
    draft("math_reduction", "这样求和后会变成什么形状？", status="clarify", context=("@由于输入矩阵沿0轴降维以生成输出向量", "@指定`axis=1`将通过汇总所有列的元素降维"), history=("我想对一个矩阵使用 sum，可能用 axis=0，也可能用 axis=1，还没确定是否保留维度。",), missing=("需澄清原矩阵形状、求和轴与是否保留维度。",)),
    draft("math_dot", "两个等长向量的点积怎样由按元素乘法与求和得到？", [("@给定两个向量", "对应位置元素相乘后求和，即两向量点积。")]),
    draft("math_dot", "什么时候点积表达的加权和可以称为加权平均？", [("@当权重为非负数且和为1", "权重必须非负且总和为1。")]),
    draft("math_matmul", "m×n 矩阵与向量相乘时，向量应有多长，结果多长，每个结果元素如何计算？", [("@回顾分别在", "矩阵为 m×n 时输入向量应为 n 维。"), ("@矩阵向量积$\\mathbf{A}\\mathbf{x}$是一个长度为$m$的列向量", "结果为长度 m 的列向量，第 i 项是矩阵第 i 行与输入向量的点积。")]),
    draft("math_matmul", "本节中 5×4 的 A 与 4×3 的 B 作矩阵乘法得到什么形状，是否就是 Hadamard 积？", [("@这里的`A`是一个5行4列的矩阵", "结果是5×3矩阵。"), ("@不应与\"Hadamard积\"混淆", "矩阵乘法不应与按元素的 Hadamard 积混淆。")]),
    draft("math_norm", "本节的 L1 与 L2 范数分别如何由向量元素计算？", [("@是向量元素平方和的平方根", "L2 是元素平方和的平方根。"), ("@它表示为向量元素的绝对值之和", "L1 是元素绝对值之和。")]),
    draft("math_norm", "矩阵的 Frobenius 范数如何计算，它与向量 L2 范数有什么关系？", [("@是矩阵元素平方和的平方根", "Frobenius 范数是矩阵全部元素平方和的平方根。"), ("@它就像是矩阵形向量的$L_2$范数", "可理解为把矩阵当作向量后求 L2 范数。")]),
    draft("math_norm", "仅根据这节资料，能确定 L1 还是 L2 会让我的贷款模型测试准确率更高，以及提高多少吗？", status="insufficient", context=("@与$L_2$范数相比",), missing=("没有具体模型、数据、评估方案与实验结果，不能确定准确率优劣或提升幅度。",)),
],
"lunyu": [
    draft("lunyu_xueer", "《学而》开篇如何分别描述学习、远方朋友来访和别人不了解自己时的态度？", [("@学而时习之", "学而时习为悦，有朋远来为乐，人不知而不愠为君子态度。")]),
    draft("lunyu_xueer", "曾子‘吾日三省吾身’具体反省哪三件事？", [("@吾日三省吾身", "为人谋是否忠，与朋友交是否信，所传是否习。")]),
    draft("lunyu_xueer", "子贡说‘贫而无谄，富而无骄’，孔子又提出怎样的进一步要求？", [("@贫而无谄", "孔子认为可，但不如贫而乐、富而好礼。")]),
    draft("lunyu_xueer", "‘学而时习之’规定了每天复习多少分钟、间隔几天复习一次吗？请给出具体数值。", status="insufficient", context=("@学而时习之",), missing=("该语句没有给出分钟数和按天的固定复习间隔，不应编造数值。",)),
    draft("lunyu_weizheng", "‘温故而知新’之后，孔子认为可以承担什么角色？", [("@温故而知新", "可以为师。")]),
    draft("lunyu_weizheng", "孔子如何分别指出只学不思和只思不学的问题？", [("@学而不思则罔", "学而不思则罔，思而不学则殆。")]),
    draft("lunyu_weizheng", "‘知之为知之’要求怎样面对知道和不知道的事？", [("@知之为知之", "知道就承认知道，不知道就承认不知道。")]),
    draft("lunyu_weizheng", "‘人而无信，不知其可也’后面用了什么比喻说明诚信的重要？", [("@人而无信", "以大车、小车缺少关键连接部件而无法行进作比喻；引用以本版本原文为准。")]),
    draft("lunyu_bayi", "《八佾》中‘人而不仁’与礼、乐之间提出了什么问题？", [("@人而不仁，如礼何", "没有仁，礼和乐又如何成立或发挥意义；原文连问如礼何、如乐何。")]),
    draft("lunyu_bayi", "子贡想去掉告朔的饩羊时，孔子如何区分两人所珍惜的对象？", [("@子贡欲去告朔之饩羊", "子贡爱其羊，孔子爱其礼。")]),
    draft("lunyu_liren", "‘君子喻于义，小人喻于利’怎样对举君子与小人？", [("@君子喻于义", "君子明于义，小人明于利。")]),
    draft("lunyu_liren", "见到贤者和不贤者时，孔子分别要求怎样反省或行动？", [("@见贤思齐焉", "见贤思齐，见不贤则内自省。")]),
    draft("lunyu_liren", "父母健在时，原文对远游有什么要求？这里的‘远’究竟是多少公里？", [("@父母在，不远游", "父母在，不远游；如出游则必有方。")], status="partial", missing=("原文没有给出‘远’的公里阈值，不能自行量化。",)),
    draft("lunyu_shuer", "《述而》‘默而识之’一章并列了哪些学习和教学态度？", [("@默而识之", "默而识之、学而不厌、诲人不倦。")]),
    draft("lunyu_shuer", "‘三人行，必有我师焉’要求如何分别对待别人的善与不善？", [("@三人行，必有我师焉", "择其善者而从之，对其不善者反省改正自己。")]),
    draft("lunyu_shuer", "子路问带领三军会与谁同行时，孔子拒绝哪种人、选择哪种人？", [("@暴虎冯河", "不与暴虎冯河、死而不悔的鲁莽者同行；选择临事而惧、好谋而成者。")]),
    draft("lunyu_zihan", "孔子在川上说‘逝者如斯夫’后，怎样描述其持续状态？", [("@逝者如斯夫", "不舍昼夜。")]),
    draft("lunyu_zihan", "‘岁寒’之后才显现出松柏的什么特点？", [("@岁寒，然后知松柏之后雕也", "岁寒后才知道松柏后凋的特点，文字依本版为‘后雕’。")]),
    draft("lunyu_yanyuan", "子贡问政时列出哪些要素？不得已删减时，孔子先去什么、再去什么，最后强调什么？", [("@足食，足兵，民信之矣", "足食、足兵、民信；不得已先去兵，再去食，强调民无信不立。")]),
    draft("lunyu_yanyuan", "他这里说的‘这个’是什么意思？", status="clarify", context=("@出门如见大宾", "@民无信不立"), history=("我同时读到‘己所不欲，勿施于人’和‘民无信不立’，想理解这两句话。",), missing=("需澄清要解释的是哪句话，不能任意替用户选择指代。",)),
],
}

HELDOUT_FAMILIES = {"law_internet", "law_judicial", "math_dot", "math_matmul", "math_norm",
                    "lunyu_shuer", "lunyu_zihan", "lunyu_yanyuan"}


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _normalized(text):
    return "".join(text.split())


def _corpus(path):
    raw = path.read_bytes()
    raw_hash = sha256(raw).hexdigest()
    original = MaterialParser().parse(raw, filename=path.name)
    chunks = [replace(c, id=f"draft-{raw_hash[:20]}-{i}") for i, c in enumerate(original)]
    version = MaterialVersion(f"draft-version-{raw_hash[:32]}", f"draft-material-{raw_hash[:32]}", path.name,
        "application/octet-stream", raw_hash, len(raw), "ready", datetime(2000, 1, 1, tzinfo=timezone.utc), chunks)
    semantic = semantic_chunks(version, f"draft-retrieval-{raw_hash[:32]}", max_tokens=1800)
    by_id = {c.id: (i, c) for i, c in enumerate(chunks)}
    def reference(span, *, quote=True):
        i, chunk = by_id[span["block"]]
        result = dict(span, material_id=version.material_id, source_filename=path.name,
                      raw_source_sha256=raw_hash, source_ordinal=i,
                      artifact_path=f"sources/{raw_hash}-{i}.txt")
        if quote:
            result["quote"] = chunk.text[span["start"]:span["end"]]
        return result
    allowed = [reference(dict(material_version_id=version.id, artifact_hash=c.content_hash,
                    start=0, end=len(c.text), page=c.page, block=c.id), quote=False) for c in chunks]
    def resolve(anchor):
        if not anchor.startswith("@"):
            raise ValueError("draft selectors must explicitly select a semantic unit")
        needle = _normalized(anchor[1:])
        article = needle.startswith("第") and needle.endswith("条")
        matches = [c for c in semantic if (_normalized(c["source_text"]).startswith(needle) if article
                                           else needle in _normalized(c["source_text"]))]
        if len(matches) != 1:
            raise ValueError(f"source anchor must match exactly once: {path.name}: {anchor}: {len(matches)}")
        return [reference(span) for span in matches[0]["source_spans"]]
    artifacts = {f"sources/{raw_hash}-{i}.txt": c.text.encode("utf-8") for i, c in enumerate(chunks)}
    manifest = dict(source_filename=path.name, raw_source_sha256=raw_hash, raw_size_bytes=len(raw),
                    material_id=version.material_id, material_version_id=version.id,
                    source_artifacts=allowed, legacy_artifact_count=len(chunks), semantic_chunk_count=len(semantic))
    return raw_hash, allowed, resolve, artifacts, manifest


def prepare(sample_dir, output_dir):
    sample_dir, output_dir = Path(sample_dir), Path(output_dir)
    rows, files, manifests = [], {}, []
    for document, filename in SAMPLES.items():
        raw_hash, allowed, resolve, artifacts, manifest = _corpus(sample_dir / filename)
        files.update(artifacts)
        manifests.append(manifest)
        for index, spec in enumerate(SPECS[document], 1):
            qid = f"real-{document}-{index:02}"
            evidence, points = [], []
            for point_number, (anchor, answer) in enumerate(spec["facts"], 1):
                identifiers = []
                for span in resolve(anchor):
                    eid = f"{qid}-e{len(evidence) + 1}"
                    evidence.append(dict(span, evidence_id=eid))
                    identifiers.append(eid)
                points.append(dict(point_id=f"{qid}-p{point_number}", text=answer,
                                   evidence_ids=identifiers, required=True))
            status = spec["status"]
            behavior = (["仅回答资料支持的部分，明确其余所需信息不足，不补造数值、地址或联系方式。"] if status == "partial" else
                        ["说明当前资料与问题信息不足以支持所问事实，不依赖外部知识补答。"] if status == "insufficient" else
                        ["先询问缺失指代或条件，不替用户选择一种解释并直接作答。"] if status == "clarify" else [])
            forbidden = (["未澄清指代或条件就任意选择一种解释并作确定回答。"] if status == "clarify" else
                         ["编造缺失信息，给出资料未支持的具体数值、地址、联系方式或实验结果。"]
                         if status in {"partial", "insufficient"} else [])
            categories = list(spec["categories"]) + ([status] if status != "answered" else ["source_grounded"])
            if len({e["page"] for e in evidence}) > 1:
                categories.append("cross_page")
            rows.append(dict(schema_version=1, question_id=qid, family_id=spec["family"], source_kind="real_material",
                synthetic=False, human_review_status="pending", source_filename=filename, raw_source_sha256=raw_hash,
                question=spec["question"], conversation_history=[{"role": "user", "content": h} for h in spec["history"]],
                categories=categories, scope_snapshot_id=f"draft-scope-{raw_hash[:32]}",
                allowed_source_spans=allowed, excluded_source_spans=[], necessary_evidence=evidence,
                context_evidence=[s for anchor in spec["context"] for s in resolve(anchor)],
                required_answer_points=points, answerability={"answered": "answerable", "partial": "partially_answerable",
                    "insufficient": "unanswerable_in_scope", "clarify": "ambiguous"}[status],
                expected_answer_status=status, missing_answer_points=list(spec["missing"]), required_behavior=behavior,
                forbidden_claims=forbidden, label_origin="agent-authored draft; requires independent human review"))
    split = dict(schema_version=1, dataset="dataset.jsonl", source_kind="real_material", human_review_status="pending",
                 purpose="review_pending_real_material_drafts", real_material_holdout_status="draft_not_run")
    for partition in ("dev", "heldout"):
        selected = [r for r in rows if (r["family_id"] in HELDOUT_FAMILIES) == (partition == "heldout")]
        split[partition] = dict(family_ids=sorted({r["family_id"] for r in selected}),
                                question_ids=[r["question_id"] for r in selected])
    files["dataset.jsonl"] = ("\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n").encode("utf-8")
    files["split.json"] = _json_bytes(split)
    files["source-manifest.json"] = _json_bytes(dict(schema_version=1, identity_status="deterministic_fixture_placeholders",
        human_review_status="pending", offset_unit="Unicode code points; half-open interval", sources=manifests))
    files["README.md"] = README.encode("utf-8")
    for name, raw in files.items():
        destination = output_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith("sources/") and destination.exists() and destination.read_bytes() != raw:
            raise ValueError("immutable source artifact would change")
        destination.write_bytes(raw)
    result = validate(output_dir)
    (output_dir / "validation-report.json").write_bytes(_json_bytes(result))
    return result


def validate(root):
    root = Path(root)
    rows = [json.loads(line) for line in (root / "dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    split = json.loads((root / "split.json").read_text(encoding="utf-8"))
    _validate_split(rows, split)
    if (len(rows) != 60 or sorted(Counter(r["source_filename"] for r in rows).values()) != [20, 20, 20]
            or any(r["human_review_status"] != "pending" or r["synthetic"] is not False for r in rows)):
        raise ValueError("draft count or review labels invalid")
    cached, verified = {}, 0
    resolved_root = root.resolve()
    for ref in _source_paths(rows):
        name = ref["artifact_path"]
        if name not in cached:
            path = (root / name).resolve()
            if not path.is_relative_to(resolved_root):
                raise ValueError("source path escapes draft directory")
            raw = path.read_bytes()
            cached[name] = sha256(raw).hexdigest(), raw.decode("utf-8")
        digest, text = cached[name]
        if (digest != ref["artifact_hash"] or not 0 <= ref["start"] < ref["end"] <= len(text)
                or ("quote" in ref and text[ref["start"]:ref["end"]] != ref["quote"])):
            raise ValueError("source hash, offset or quote mismatch")
        verified += 1
    for row in rows:
        evidence = {e["evidence_id"]: e for e in row["necessary_evidence"]}
        if len(evidence) != len(row["necessary_evidence"]):
            raise ValueError("duplicate evidence identity")
        for point in row["required_answer_points"]:
            if not point["evidence_ids"] or not set(point["evidence_ids"]) <= evidence.keys():
                raise ValueError("answer point lacks verifiable evidence")
        allowed = {s["block"]: s for s in row["allowed_source_spans"]}
        for e in [*row["necessary_evidence"], *row["context_evidence"]]:
            a = allowed.get(e["block"])
            if a is None or a["artifact_hash"] != e["artifact_hash"] or not a["start"] <= e["start"] < e["end"] <= a["end"]:
                raise ValueError("gold source is outside permitted document")
    return dict(questions=len(rows), documents=len({r["source_filename"] for r in rows}),
                dev=len(split["dev"]["question_ids"]), heldout=len(split["heldout"]["question_ids"]),
                families=len({r["family_id"] for r in rows}), verified_spans=verified, source_artifacts=len(cached),
                expected_status_counts=dict(Counter(r["expected_answer_status"] for r in rows)),
                human_review_status="pending", heldout_executed=False, providers_called=False,
                checks="Exact artifact hash, UTF-8 quote, Unicode offsets, allowed-scope coverage, answer-point references and family split only; no human semantic approval.")


README = """# 真实资料评测草稿：全部待人工审核

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
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.check and args.sample_dir is None:
        parser.error("--sample-dir is required for generation")
    result = validate(args.output) if args.check else prepare(args.sample_dir, args.output)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
