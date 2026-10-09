import httpx,json
urls=[
'https://api.github.com/repos/ctf-wiki/ctf-wiki/git/trees/master?recursive=1',
'https://api.github.com/repos/ThinkInAIXYZ/deepseek-free-api',
'https://www.gutenberg.org/cache/epub/23962/pg23962.txt',
'https://www.gutenberg.org/cache/epub/23839/pg23839.txt',
'https://raw.githubusercontent.com/zhanggyb/ebook/master/README.md',
'https://api.github.com/repos/AllenDowney/ThinkPython2/git/trees/master?recursive=1',
'https://api.github.com/repos/apachecn/think-py-2e-zh/git/trees/master?recursive=1',
'https://api.github.com/repos/it-ebooks-0/it-ebooks-2018/git/trees/master?recursive=1'
]
for url in urls:
 try:
  r=httpx.get(url,follow_redirects=True,timeout=20)
  if 'trees/' in url and r.status_code==200:
   data=[i['path'] for i in r.json().get('tree',[]) if i['path'].lower().endswith('.pdf')]
   print(json.dumps({'url':url,'status':r.status_code,'pdfs':data[:40]},ensure_ascii=True))
  else: print(json.dumps({'url':url,'status':r.status_code,'sample':r.text[:500]},ensure_ascii=True))
 except Exception as e: print(type(e).__name__)
