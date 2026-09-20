"""Read-only service diagnostics with allowlisted, credential-free output."""
import json
import time
import httpx
from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.rag.reranking import DashScopeReranker


def main():
    import argparse
    from dotenv import load_dotenv
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--endpoint')
    args = parser.parse_args()
    load_dotenv(args.env_file)
    observations = []
    def observed(response):
        response.read()
        try:
            code = response.json().get('code')
        except (ValueError, AttributeError):
            code = 'non_json'
        # Never print provider messages/request IDs or exception bodies.
        observations.append({'http_status': response.status_code,
            'provider_code': code if isinstance(code, str) and code.replace('.', '').replace('_', '').isalnum() else None})
    with httpx.Client(event_hooks={'response': [observed]}, follow_redirects=False) as client:
        adapter = DashScopeReranker(LearningSettings.from_env(), client=client, endpoint=args.endpoint)
        try:
            result = adapter.rerank('学校通知谁？', [{'retrieval_text': '学校应当通知监护人。'}],
                                    deadline=time.monotonic()+30)
            status = 'ok' if len(result) == 1 else 'invalid'
        except Exception as error:
            status = getattr(error, 'code', 'PROBE_FAILED')
        print(json.dumps({'status': status, 'observations': observations}))


if __name__ == '__main__':
    main()
