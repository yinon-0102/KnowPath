"""Frozen wire and counting settings shared by runtime and evaluation."""
import os

from .token_budget import ConservativeByteProfile, load_verified_local_profile


def counting_profile(settings):
    manifest, digest = os.getenv('RAG_TOKEN_PROFILE_MANIFEST'), os.getenv('RAG_TOKEN_PROFILE_SHA256')
    if not manifest and not digest:
        return ConservativeByteProfile(settings.chat_provider, settings.chat_model)
    if not manifest or not digest: raise ValueError('RAG_TOKEN_PROFILE_INVALID')
    try:
        return load_verified_local_profile(manifest, expected_manifest_sha256=digest,
            provider=settings.chat_provider, model=settings.chat_model)
    except (ValueError, TypeError, OSError, ImportError, KeyError):
        raise ValueError('RAG_TOKEN_PROFILE_INVALID') from None


def protocol_configuration(settings):
    try:
        values = {}
        for name, key, default, minimum, maximum in (
            ('max_draft_bytes','RAG_MAX_DRAFT_BYTES','4000',256,16000),
            ('revision_generation_seconds','RAG_REVISION_GENERATION_SECONDS','10',1,120),
            ('revision_verification_seconds','RAG_REVISION_VERIFICATION_SECONDS','25',1,120),
        ):
            raw = os.getenv(key, default)
            if not raw.isascii() or not raw.isdecimal(): raise ValueError()
            value = int(raw)
            if not minimum <= value <= maximum: raise ValueError()
            values[name] = value
        mode = os.getenv('RAG_RESPONSE_FORMAT','json_object')
        if mode not in {'json_object','json_schema'}: raise ValueError()
        return {**values,'response_format':mode,'wire_version':2,'evidence_locator':'segment-id-v1',
                'generator_evidence':'whole-source-v1', 'identity_schema':'request-enum-v1',
                'math_notation':'unicode-plain-v1',
                'counting_profile':counting_profile(settings).provenance}
    except (ValueError, TypeError, AttributeError):
        raise ValueError('RAG_PROTOCOL_CONFIG_INVALID') from None
