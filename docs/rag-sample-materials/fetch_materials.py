import concurrent.futures, hashlib, io, json, sys
from pathlib import Path
import httpx
from pypdf import PdfReader
sys.path.insert(0, str(Path.cwd()))
from knowpath_backend.learning.materials.service import MaterialParser
out = Path('../docs/rag-sample-materials')
items = [
 ('think-python-2e.pdf', 'Think Python, 2nd edition', 'https://greenteapress.com/thinkpython2/thinkpython2.pdf'),
 ('think-stats-2e.pdf', 'Think Stats, 2nd edition', 'https://greenteapress.com/thinkstats2/thinkstats2.pdf'),
 ('d2l-linear-algebra-zh.md', '动手学深度学习：线性代数', 'https://raw.githubusercontent.com/d2l-ai/d2l-zh/master/chapter_preliminaries/linear-algebra.md'),
 ('d2l-calculus-zh.md', '动手学深度学习：微积分', 'https://raw.githubusercontent.com/d2l-ai/d2l-zh/master/chapter_preliminaries/calculus.md'),
 ('rfc8259-json.txt', 'RFC 8259: JSON', 'https://www.rfc-editor.org/rfc/rfc8259.txt'),
]
def fetch(item):
 name, title, url = item
 try:
  r = httpx.get(url, follow_redirects=True, timeout=60)
  r.raise_for_status()
  data = r.content
  chunks = MaterialParser().parse(data, filename=name)
  result = dict(file=name,title=title,source=url,final_url=str(r.url),bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),chunks=len(chunks),max_chunk_chars=max(len(c.text) for c in chunks),chunks_over_6000=sum(len(c.text)>6000 for c in chunks),sample=chunks[0].text[:180])
  if name.endswith('.pdf'):
   reader = PdfReader(io.BytesIO(data))
   result['pages']=len(reader.pages)
   result['pages_with_text']=sum(bool((p.extract_text() or '').strip()) for p in reader.pages)
  (out/name).write_bytes(data)
  return result
 except Exception as e:
  return dict(file=name,source=url,error=str(e))
with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
 results=list(pool.map(fetch,items))
(out/'manifest.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=True,indent=2))
