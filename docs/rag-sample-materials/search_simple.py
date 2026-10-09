import httpx,re,html,concurrent.futures
urls=[('https://html.duckduckgo.com/html/',{'q':'安全 手册 filetype:pdf 48'}),('https://www.baidu.com/s',{'wd':'科普 手册 filetype:pdf 48'}),('https://cn.bing.com/search',{'q':'"应急" "手册" "pdf"','setlang':'zh-hans','cc':'cn'})]
def run(it):
 u,p=it
 try:
  r=httpx.get(u,params=p,follow_redirects=True,timeout=25,headers={'User-Agent':'Mozilla/5.0'})
  t=re.sub(r'<(script|style)\b[^>]*>.*?</\1>','',r.text,flags=re.S)
  print(u,r.status_code,html.unescape(re.sub('<[^>]+>',' ',t))[:15000]); print('LINKS',re.findall(r'href=[\"\x27]([^\"\x27]+)',r.text)[:80])
 except Exception as e: print(type(e).__name__)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p:list(p.map(run,urls))
