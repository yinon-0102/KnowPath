import httpx,io,json,concurrent.futures
from pypdf import PdfReader
from pathlib import Path
base='https://www.cfs.gov.hk/sc_chi/'
paths=['consumer_zone/safefood_all/files/booklet.pdf','trade_zone/safe_kitchen/booklet.pdf','multimedia/multimedia_pub/files/5keys_bk_Public_C.pdf','multimedia/multimedia_pub/files/FoodHandleHygiene_guidebook.pdf','food_leg/files/5_keys_brochure_c.pdf','food_leg/files/A_Practical_Guide_for_Residential_Care_Homes_TC.pdf']
p=Path('../docs/rag-sample-materials/candidates');p.mkdir(exist_ok=True)
def check(path):
 try:
  r=httpx.get(base+path,follow_redirects=True,timeout=45);r.raise_for_status();pdf=PdfReader(io.BytesIO(r.content));s='\n'.join(x.extract_text() or '' for x in pdf.pages[:3])
  name=path.replace('/','_');(p/name).write_bytes(r.content)
  return dict(file=name,url=base+path,pages=len(pdf.pages),bytes=len(r.content),sample=s[:650])
 except Exception as e:return dict(url=base+path,error=str(e))
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:print(json.dumps(list(pool.map(check,paths)),ensure_ascii=False,indent=2))
