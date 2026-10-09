import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

root = next(parent for parent in Path(__file__).resolve().parents if (parent / 'py/knowpath_backend').is_dir())
sys.path.insert(0, str(root / 'py'))
from knowpath_backend.learning.materials.service import MaterialParser

path = root / 'docs/rag-sample-materials/chinese/minors-protection-law-2024.pdf'
data = path.read_bytes()
reader = PdfReader(path)
texts = [p.extract_text() or '' for p in reader.pages]
chunks = MaterialParser().parse(data, filename=path.name)
image_counts = []
paint_counts = []
for page in reader.pages:
    images = 0
    def count_images(resources, seen):
        result = 0
        for ref in resources.get('/XObject', {}).get_object().values() if '/XObject' in resources else []:
            obj = ref.get_object()
            key = id(obj)
            if key in seen:
                continue
            seen.add(key)
            if obj.get('/Subtype') == '/Image':
                result += 1
            elif obj.get('/Subtype') == '/Form':
                result += count_images(obj.get('/Resources', {}).get_object() if '/Resources' in obj else {}, seen)
        return result
    images = count_images(page['/Resources'].get_object(), set())
    image_counts.append(images)
    ops = Counter(op.decode('ascii', errors='replace') for _, op in page.get_contents().operations)
    paint_counts.append({key: ops[key] for key in ['Do', 'INLINE IMAGE', 'S', 's', 'f', 'F', 'f*', 'B', 'B*'] if ops[key]})

chapters = []
articles = []
for page, text in enumerate(texts, 1):
    for match in re.finditer(r'(?m)^第([一二三四五六七八九十百零〇]+)章\s*([^\n]+)', text):
        chapters.append({'page':page, 'title':match.group(0)})
    articles.extend(re.findall(r'(?m)^第([一二三四五六七八九十百零〇]+)条', text))

def chinese_number(value):
    digits = {'零':0, '〇':0, '一':1, '二':2, '三':3, '四':4, '五':5, '六':6, '七':7, '八':8, '九':9}
    total = 0
    current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        else:
            total += (current or 1) * {'十':10, '百':100}[char]
            current = 0
    return total + current

article_numbers = list(map(chinese_number, articles))
stats = {
    'file': path.name,
    'title': '中华人民共和国未成年人保护法（2024年修正版）',
    'source_page': 'https://cgzf.sh.gov.cn/channel_87/20241124/50ee5201540047278966e83fbcb3d4da.html',
    'source': 'https://cgzf.sh.gov.cn/cmsres/30/3070ca9c07c14f3fa5d1468cac0010cf/3fb1b7cd7118df6e3df3fd19a420dfb1.pdf',
    'downloaded_at': '2026-09-20',
    'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
    'pages': len(texts), 'pages_with_text': sum(bool(t.strip()) for t in texts),
    'page_sizes_pt': sorted(set((round(float(p.mediabox.width),2),round(float(p.mediabox.height),2)) for p in reader.pages)),
    'page_rotations': sorted(set(p.rotation for p in reader.pages)),
    'image_xobjects_per_page': image_counts,
    'drawing_operations_per_page': paint_counts,
    'text_chars_per_page': [len(t) for t in texts],
    'replacement_characters': sum(t.count('\ufffd') for t in texts),
    'chapters': chapters,
    'article_headings': len(articles),
    'article_sequence_1_to_132': article_numbers == list(range(1,133)),
    'chunks': len(chunks),
    'min_chunk_chars': min(len(c.text) for c in chunks),
    'max_chunk_chars': max(len(c.text) for c in chunks),
    'chunk_pages': sorted(set(c.page for c in chunks)),
    'chunks_with_section_path': sum(bool(c.section_path) for c in chunks),
    'sample': texts[1][:500],
    'verification': '逐页文本提取、章节及条文顺序、项目 MaterialParser；37页渲染缩略图检查单栏文字布局。未开展真实模型回答评测。',
    'limitation': '当前项目PDF解析主要按页/空行分块，尚不能自动得到章条结构；跨页条款仍可能拆开。37页略少于此前40至50页的偏好。',
}
assert stats['pages'] == 37
assert stats['pages_with_text'] == 37
assert stats['article_sequence_1_to_132'], article_numbers
assert len(chapters) == 9, chapters
assert sum(image_counts) == 0
assert stats['replacement_characters'] == 0
assert stats['chunk_pages'] == list(range(1,38))
(Path(__file__).parent / 'minors-verification.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({k:v for k,v in stats.items() if k not in ['sample','text_chars_per_page','image_xobjects_per_page','drawing_operations_per_page','chunk_pages']},ensure_ascii=True),flush=True)
