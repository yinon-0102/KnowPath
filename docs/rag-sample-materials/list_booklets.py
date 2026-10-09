import httpx,re,html
u='https://www.cfs.gov.hk/sc_chi/multimedia/multimedia_pub/multimedia_pub_booklet.html'
r=httpx.get(u,timeout=25)
from urllib.parse import urljoin
for href,label in re.findall(r'<a\b[^>]*href=[\"\x27]([^\"\x27]+)[\"\x27][^>]*>(.*?)</a>',r.text,re.S|re.I):
 if '.pdf' in href.lower():print(urljoin(u,href),html.unescape(re.sub('<[^>]+>','',label)).strip())
