import sys,json,hashlib,shutil
from pathlib import Path
sys.path.insert(0,str(Path('../docs/rag-sample-materials/conversion-tools').resolve()))
sys.path.insert(0,str(Path.cwd()))
from opencc import OpenCC
from pypdf import PdfReader
from knowpath_backend.learning.materials.service import MaterialParser
root=Path('../docs/rag-sample-materials');out=root/'chinese'
archive=root/'previous-chinese';archive.mkdir(exist_ok=True)
for name in ['ctex-manual-zh.pdf','lunyu-zh.txt']:
 source=out/name
 if source.exists(): shutil.move(str(source),str(archive/name))
pdf=out/'food-safety-guide-zh.pdf'
shutil.copyfile(root/'candidates/food_leg_files_A_Practical_Guide_for_Residential_Care_Homes_TC.pdf',pdf)
original=(archive/'lunyu-zh.txt').read_text(encoding='utf-8-sig')
converted=OpenCC('t2s').convert(original)
assert '学而时习之' in converted
(out/'lunyu-zh-hans.txt').write_text(converted,encoding='utf-8')
rows=[]
for name,title,url in [('food-safety-guide-zh.pdf','确保院舍食物安全实务指南（繁体中文）','https://www.cfs.gov.hk/sc_chi/food_leg/files/A_Practical_Guide_for_Residential_Care_Homes_TC.pdf'),('linear-algebra-zh.md','动手学深度学习：线性代数','https://raw.githubusercontent.com/d2l-ai/d2l-zh/master/chapter_preliminaries/linear-algebra.md'),('lunyu-zh-hans.txt','论语（简体转换版）','https://www.gutenberg.org/cache/epub/23839/pg23839.txt')]:
 f=out/name;data=f.read_bytes();chunks=MaterialParser().parse(data,filename=name)
 row=dict(file=name,title=title,source=url,bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),chunks=len(chunks),max_chunk_chars=max(len(c.text) for c in chunks))
 if name.endswith('.pdf'):
  reader=PdfReader(f);texts=[p.extract_text() or '' for p in reader.pages]
  row.update(pages=len(reader.pages),pages_with_text=sum(bool(t.strip()) for t in texts),sample=texts[2][:400])
 if name.endswith('.txt'):row.update(transformation='OpenCC 0.1.7 t2s; retain original content and license notices',sample=converted[converted.index('学而时习之')-10:converted.index('学而时习之')+130])
 rows.append(row)
(out/'manifest.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(rows,ensure_ascii=False,indent=2))
