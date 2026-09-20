"""Closed diagnostic vocabulary; no input values or arbitrary field names."""

SCHEMA_ERROR_TYPES = frozenset({
    'missing', 'extra_forbidden', 'literal_error', 'string_type', 'string_too_short',
    'string_too_long', 'tuple_type', 'list_type', 'dict_type', 'model_type',
    'int_type', 'int_parsing', 'bool_type', 'bool_parsing', 'too_short', 'too_long',
    'greater_than', 'greater_than_equal', 'less_than', 'less_than_equal',
    'value_error', 'invariant', 'other', 'duplicate_claim_identity',
    'duplicate_point_identity', 'dependency_order', 'nonanswer_contains_claims',
    'answer_without_claims',
})

SCHEMA_ERROR_PATHS = {'$', 'status', 'claims', 'required_points', 'missing_points',
                      'missing_points[]', 'checks', 'requirement_checks', 'complete', 'reason_code'}
for collection, fields in {
    'claims': ('claim_id', 'text', 'kind', 'citation_ids', 'depends_on'),
    'required_points': ('point_id', 'text'),
    'checks': ('claim_id', 'status', 'citation_ids', 'reason', 'issue_type',
               'qualifier_checks', 'evidence_spans'),
    'requirement_checks': ('point_id', 'text', 'status', 'citation_ids', 'claim_ids', 'reason'),
}.items():
    SCHEMA_ERROR_PATHS.add(collection + '[]')
    SCHEMA_ERROR_PATHS.update(collection + '[].' + field for field in fields)
for path in ('claims[].citation_ids', 'claims[].depends_on', 'checks[].citation_ids',
             'requirement_checks[].citation_ids', 'requirement_checks[].claim_ids'):
    SCHEMA_ERROR_PATHS.add(path + '[]')
SCHEMA_ERROR_PATHS.update('checks[].qualifier_checks.' + field for field in
                         ('subject', 'conditions', 'exceptions', 'negation', 'quantifiers'))
SCHEMA_ERROR_PATHS.update({'checks[].evidence_spans[]', 'checks[].evidence_spans[].evidence_id',
                          'checks[].evidence_spans[].citation_id', 'checks[].evidence_spans[].start',
                          'checks[].evidence_spans[].end'})
SCHEMA_ERROR_PATHS = frozenset(SCHEMA_ERROR_PATHS)

INVARIANT_PATHS = {
    'duplicate_claim_identity':'claims', 'duplicate_point_identity':'required_points',
    'dependency_order':'claims[].depends_on', 'nonanswer_contains_claims':'claims',
    'answer_without_claims':'claims',
}


def safe_schema_location(location):
    """Return only a known path, truncating unknown extras to a known parent."""
    if not isinstance(location, (tuple, list)):
        return '$'
    path = '$'
    for part in location:
        if type(part) is int and part >= 0:
            candidate = path + '[]'
        elif type(part) is str and part.isidentifier():
            candidate = part if path == '$' else path + '.' + part
        else:
            break
        if candidate not in SCHEMA_ERROR_PATHS:
            break
        path = candidate
    return path
