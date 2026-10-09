import concurrent.futures, httpx, json, re
from pathlib import Path
from html.parser import HTMLParser
class Reader(HTMLParser):
 def __init__(self):super().__init__();self.parts=[];self.skip=0
 def handle_starttag(self,t,a):
  if t in ('script','style'):self.skip+=1
  if t in ('p','h1','h2','h3','li','br','section'):self.parts.append('\n')
 def handle_endtag(self,t):
  if t in ('script','style'):self.skip=max(0,self.skip-1)
 def handle_data(self,s):
  if not self.skip:self.parts.append(s)
out=Path('../docs/research/tree-rag')
urls={
'raptor-paper':'https://arxiv.org/html/2401.18059v1',
'raptor-repo':'https://raw.githubusercontent.com/parthsarthi03/raptor/master/README.md',
'llama-automerge':'https://raw.githubusercontent.com/run-llama/llama_index/main/llama-index-core/llama_index/core/retrievers/auto_merging_retriever.py',
'llama-hierarchy':'https://raw.githubusercontent.com/run-llama/llama_index/main/llama-index-core/llama_index/core/node_parser/relational/hierarchical.py',
'langchain-parent':'https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/classic/langchain_classic/retrievers/parent_document_retriever.py',
'docling':'https://raw.githubusercontent.com/docling-project/docling/main/README.md',
'docling-chunking':'https://docling-project.github.io/docling/concepts/chunking/',
'mineru':'https://raw.githubusercontent.com/opendatalab/MinerU/master/README.md',
}
def fetch(item):
 name,url=item
 try:
  r=httpx.get(url,follow_redirects=True,timeout=35);r.raise_for_status();s=r.text
  if '<html' in s[:2000].lower():
   p=Reader();p.feed(s);s=''.join(p.parts)
  s=re.sub(r'[ \t]+',' ',s);s=re.sub(r'\n{3,}','\n\n',s)
  (out/(name+'.txt')).write_text(s,encoding='utf-8')
  return dict(name=name,url=url,final_url=str(r.url),length=len(s),sample=s[:1400])
 except Exception as e:return dict(name=name,url=url,error=str(e))
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(fetch,urls.items()))
(out/'sources.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=True,indent=2))
