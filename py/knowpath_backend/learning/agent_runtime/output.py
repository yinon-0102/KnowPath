"""Strict Agent output; evidence IDs are aliases, never caller authority."""
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from knowpath_backend.learning.rag.verification import Claim, Draft, RequiredPoint


class SemanticRoute(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    document_related: bool
    educational: bool
    topic_ids: list[str] = Field(max_length=128)
    scope_uncertain: bool
    reason: str = Field(min_length=1, max_length=1000)


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    route: Literal['general', 'material']
    text: str = Field(min_length=1, max_length=16000, strict=True)
    status: Literal['answered', 'partial', 'insufficient', 'clarify']
    claims: list[Claim] = Field(max_length=12)
    required_points: list[RequiredPoint] = Field(min_length=1, max_length=12)
    missing_points: list[str] = Field(max_length=12)
    semantic_route: SemanticRoute | None = None


def parse_output(text, *, allowed_topic_ids=()):
    if not isinstance(text, str) or len(text.encode('utf-8')) > 64000:
        raise ValueError('Invalid Agent candidate output')
    try:
        result = AgentOutput.model_validate_json(text)
        if result.route == 'general' and (result.claims or result.missing_points):
            raise ValueError('General answer cannot contain material claims or citations')
        if result.route == 'general' and re.search(r'\bE\d+\b', result.text):
            raise ValueError('General answer cannot cite evidence')
        if result.semantic_route is not None:
            semantic = result.semantic_route
            if semantic.document_related != (result.route == 'material'):
                raise ValueError('Semantic route conflicts with evidence route')
            if (len(set(semantic.topic_ids)) != len(semantic.topic_ids)
                    or not set(semantic.topic_ids) <= set(allowed_topic_ids)):
                raise ValueError('Semantic route contains unauthorized topic IDs')
            if not semantic.educational and (semantic.topic_ids or semantic.scope_uncertain):
                raise ValueError('Noneducational semantic route cannot claim topic assistance')
        return result
    except ValidationError:
        raise ValueError('Invalid Agent candidate schema') from None


def resolve_draft(output, registry):
    records = output.model_dump(exclude={'route', 'text', 'semantic_route'})
    sources = []
    for claim in records['claims']:
        identifiers = claim['citation_ids']
        try:
            originals = registry.resolve(identifiers)
        except Exception:
            raise ValueError('Unknown candidate citation identifier') from None
        claim['citation_ids'] = [source['chunk_id'] for source in originals]
        for source in originals:
            if source not in sources:
                sources.append(source)
    try:
        return Draft.model_validate(records), sources
    except ValidationError:
        raise ValueError('Invalid material candidate draft') from None
