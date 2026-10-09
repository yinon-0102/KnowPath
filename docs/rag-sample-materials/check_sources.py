import httpx, json
from pathlib import Path
from pypdf import PdfReader
out=Path('../docs/rag-sample-materials')
for name in ['think-python-2e.pdf','think-stats-2e.pdf']:
 r=PdfReader(out/name)
 print(name)
 print('\n'.join((p.extract_text() or '') for p in r.pages[:3])[:6000])
r=httpx.get('https://raw.githubusercontent.com/d2l-ai/d2l-zh/master/LICENSE',follow_redirects=True,timeout=30)
print('D2L LICENSE',r.status_code,r.text[:4000])
if r.status_code==200: (out/'D2L-LICENSE').write_bytes(r.content)
