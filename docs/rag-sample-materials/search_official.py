import httpx,re,html,concurrent.futures
from urllib.parse import urljoin
urls=['https://www.cfs.gov.hk/sc_chi/multimedia/multimedia_pub/multimedia_pub_fse.html','https://www.hko.gov.hk/sc/publica/publist.htm','https://www.hko.gov.hk/tc/publica/publist.htm','https://www.cfs.gov.hk/sc_chi/multimedia/multimedia_pub/multimedia_pub.html']
def run(u):
 try:
  r=httpx.get(u,timeout=20,follow_redirects=True);print(u,r.status_code)
  links=re.findall(r'<a\b[^>]*href=[\"\x27]([^\"\x27]+)[\"\x27][^>]*>(.*?)</a>',r.text,re.S|re.I)
  for href,label in links:
   label=html.unescape(re.sub('<[^>]+>','',label)).strip()
   if '.pdf' in href.lower() or any(x in label for x in ['册','冊','指南']):print(urljoin(str(r.url),href),label[:90])
 except Exception as e:print(type(e).__name__)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as p:list(p.map(run,urls))
