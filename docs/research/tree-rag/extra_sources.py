import httpx,concurrent.futures,json
from pathlib import Path
urls={
'langchain-parent':'https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/langchain-classic/langchain_classic/retrievers/parent_document_retriever.py',
'langchain-parent-ref':'https://reference.langchain.com/python/langchain-classic/retrievers/parent_document_retriever/ParentDocumentRetriever',
'docling-headings':'https://docling-project.github.io/docling/examples/heading_hierarchy/',
'anthropic-context':'https://www.anthropic.com/engineering/contextual-retrieval'
}
# Reuse local HTML parser helper without running the downloader module.
from html.parser import HTMLParser
class Text(HTMLParser):
 def __init__(self):super().__init__();self.parts=[];self.skip=0
 def handle_starttag(self,t,a):
  if t in ('script','style'):self.skip+=1
  elif t in ('p','h1','h2','h3','li','br'):self.parts.append('\n')
 def handle_endtag(self,t):
  if t in ('script','style'):self.skip=max(0,self.skip-1)
 def handle_data(self,s):
  if not self.skip:self.parts.append(s)
def run(it):
 n,u=it
 try:
  r=httpx.get(u,follow_redirects=True,timeout=30);r.raise_for_status();s=r.text
  if '<html' in s[:2000].lower():p=Text();p.feed(s);s=''.join(p.parts)
  Path('../docs/research/tree-rag/'+n+'.txt').write_text(s,encoding='utf-8')
  return dict(name=n,url=u,length=len(s),sample=s[:400])
 except Exception as e:return dict(name=n,url=u,error=str(e))
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as p:print(json.dumps(list(p.map(run,urls.items())),ensure_ascii=True))
