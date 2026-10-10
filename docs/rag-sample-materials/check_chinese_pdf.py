import httpx,concurrent.futures,io,json,logging
from pypdf import PdfReader
from pathlib import Path
logging.getLogger('pypdf').setLevel(logging.ERROR)
urls=[
'https://mirrors.ctan.org/language/chinese/ctex/ctex.pdf',
'https://mirrors.ctan.org/language/chinese/ctex/ctex.pdf',
'https://mirrors.ctan.org/macros/latex/contrib/ctex/ctex.pdf',
'https://www.gutenberg.org/cache/epub/23839/pg23839.txt'
]
def check(url):
 try:
  r=httpx.get(url,follow_redirects=True,timeout=40);r.raise_for_status()
  if not r.content.startswith(b'%PDF'):return dict(url=url,status=r.status_code)
  reader=PdfReader(io.BytesIO(r.content));text='\n'.join(p.extract_text() or '' for p in reader.pages[:5]);han=sum('\u4e00'<=c<='\u9fff' for c in text)
  result=dict(url=url,pages=len(reader.pages),bytes=len(r.content),han=han,sample=text[:700])
  if han>100 and len(reader.pages)<=300:
   Path('../docs/rag-sample-materials/chinese/ctex-manual-zh.pdf').write_bytes(r.content)
  return result
 except Exception as e:return dict(url=url,error=str(e))
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:print(json.dumps(list(pool.map(check,urls)),ensure_ascii=True))
