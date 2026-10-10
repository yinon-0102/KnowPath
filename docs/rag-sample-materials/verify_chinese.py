from pathlib import Path
import json,sys,hashlib
from pypdf import PdfReader
sys.path.insert(0,str(Path.cwd()))
from knowpath_backend.learning.materials.service import MaterialParser
p=Path('../docs/rag-sample-materials/chinese')
rows=json.loads((p/'manifest.json').read_text(encoding='utf-8'))[1:]
f=p/'ctex-manual-zh.pdf';data=f.read_bytes();chunks=MaterialParser().parse(data,filename=f.name)
row=dict(file=f.name,title='CTeX 宏集手册',source='https://mirrors.ctan.org/language/chinese/ctex/ctex.pdf',bytes=len(data),pages=len(PdfReader(f).pages),chunks=len(chunks),max_chunk_chars=max(len(c.text) for c in chunks),sha256=hashlib.sha256(data).hexdigest(),sample=chunks[0].text[:400])
rows.insert(0,row)
(p/'manifest.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(row,ensure_ascii=True))
