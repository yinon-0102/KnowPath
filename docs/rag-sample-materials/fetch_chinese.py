from pathlib import Path
import httpx, json, hashlib, logging
from pypdf import PdfReader
import sys
sys.path.insert(0,str(Path.cwd()))
from knowpath_backend.learning.materials.service import MaterialParser
logging.getLogger('pypdf').setLevel(logging.ERROR)
out=Path('../docs/rag-sample-materials/chinese'); out.mkdir(exist_ok=True)
items=[('lshort-zh-cn.pdf','一份不太简短的 LaTeX2e 介绍（中文）','https://mirrors.ctan.org/info/lshort/chinese/lshort-zh-cn.pdf'),('linear-algebra-zh.md','动手学深度学习：线性代数','https://raw.githubusercontent.com/d2l-ai/d2l-zh/master/chapter_preliminaries/linear-algebra.md'),('lunyu-zh.txt','论语（繁体中文）','https://www.gutenberg.org/cache/epub/23839/pg23839.txt')]
results=[]
for name,title,url in items:
 try:
  r=httpx.get(url,follow_redirects=True,timeout=60);r.raise_for_status();data=r.content
  chunks=MaterialParser().parse(data,filename=name)
  row=dict(file=name,title=title,source=url,final_url=str(r.url),bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),chunks=len(chunks),max_chunk_chars=max(len(c.text) for c in chunks),sample='\n'.join(c.text for c in chunks[:3])[:1200])
  if name.endswith('.pdf'):
   import io
   reader=PdfReader(io.BytesIO(data)); row['pages']=len(reader.pages)
  (out/name).write_bytes(data);results.append(row)
 except Exception as e:results.append(dict(file=name,error=str(e)))
(out/'manifest.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=True,indent=2))
