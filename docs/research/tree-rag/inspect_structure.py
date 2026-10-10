import json
from pathlib import Path
from pypdf import PdfReader
p=Path('../docs/rag-sample-materials/chinese/food-safety-guide-zh.pdf')
r=PdfReader(p)
print(json.dumps({'pages':len(r.pages),'outline_entries_top':len(r.outline),'page3':r.pages[2].extract_text()[:1200]},ensure_ascii=True))
