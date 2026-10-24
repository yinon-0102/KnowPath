"""Web memory is quoted data; only the caller's control prompt is a system rule."""
from dataclasses import replace
import json
from knowpath_backend.context.engine import count_tokens


class WebContextPolicy:
    whole_turns = True
    provenance = True

    def __call__(self, segments):
        result = []
        for segment in segments:
            if segment.source == 'system':
                result.append(segment)
                continue
            content = json.dumps({'type': segment.source, 'trust': 'untrusted',
                                  'id': segment.item_id, 'source_role': segment.role, 'metadata': segment.metadata,
                                  'content': segment.content}, ensure_ascii=False, allow_nan=False)
            # Use a distinct source to bypass the legacy engine's system-role grouping.
            result.append(replace(segment, source='web:' + segment.source,
                                  role='user', content=content, tokens=count_tokens(content)))
        return result
