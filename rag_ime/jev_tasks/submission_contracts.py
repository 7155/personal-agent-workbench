"""Pure proposal contracts shared by Tool disclosure and existing JEV owners."""
from __future__ import annotations

from copy import deepcopy
from itertools import islice

from .types import GraphError, canonical

MAX_ENCODED_BYTES = 64000


def _text(maximum):
    # Match types.text: nonblank, no NUL, length before stripping whitespace.
    return {'type': 'string', 'minLength': 1, 'maxLength': maximum,
            'pattern': r'^(?=[\s\S]*\S)[^\u0000]*$'}


def _refs(minimum, maximum):
    return {'type': 'array', 'minItems': minimum, 'maxItems': maximum, 'items': _text(1000)}


_SCHEMAS = {
    'result_submit': {'type': 'object', 'additionalProperties': False,
        'required': ['resultSummary', 'evidenceRefs'],
        'properties': {'resultSummary': _text(4000), 'artifactRefs': _refs(0, 16), 'evidenceRefs': _refs(1, 24)}},
    'verification_submit': {'type': 'object', 'additionalProperties': False,
        'required': ['operabilityVerdict', 'requirementVerdict', 'reason', 'evidenceRefs'],
        'properties': {
            'operabilityVerdict': {'type': 'string', 'enum': ['passed', 'failed', 'unverified']},
            'requirementVerdict': {'type': 'string', 'enum': ['satisfied', 'not_satisfied', 'unverified']},
            'reason': _text(2000), 'evidenceRefs': _refs(1, 24)}},
    'final_submit': {'type': 'object', 'additionalProperties': False,
        'required': ['content', 'evidenceRefs'],
        'properties': {'content': _text(16000), 'evidenceRefs': _refs(1, 24)}},
}
for _schema in _SCHEMAS.values():
    _schema['description'] = 'Encoded proposal must be <= 64000 UTF-8 bytes. Use only these fields; do not submit a generic AgentResult.'

def submission_contract(operation):
    """No mutable schema object or execution authority escapes this projection."""
    if operation not in _SCHEMAS:
        return None  # plan_submit retains its existing version/topology contract.
    return {'operation': operation, 'maxEncodedBytes': MAX_ENCODED_BYTES,
            'proposalSchema': deepcopy(_SCHEMAS[operation])}


def _field_help(schema):
    if 'enum' in schema:
        return 'one of ' + ', '.join(schema['enum'])
    if schema['type'] == 'array':
        return f"array {schema['minItems']}..{schema['maxItems']} items; each " + _field_help(schema['items'])
    return f"string 1..{schema['maxLength']} characters, nonblank and no NUL"


def _invalid(operation, problems):
    schema = _SCHEMAS[operation]
    # Describe the expected shape without echoing submitted bodies or extra keys.
    fields = '; '.join(name + ': ' + _field_help(value) for name, value in schema['properties'].items())
    return GraphError(f"Invalid {operation} proposal: {problems}. Required fields: "
        + ', '.join(schema['required']) + '. Only the declared fields are allowed. '
        + fields + f'. Encoded proposal must be <= {MAX_ENCODED_BYTES} UTF-8 bytes. '
        'Use room_partner op=list to recover the current submissionContract; '
        'submit observed evidence and truthful verdicts, not a generic AgentResult.')


def _problems(value, schema, path='proposal'):
    """Validate just the three owned shapes; jsonschema is a dev-only dependency."""
    kind = schema['type']
    expected = {'object': dict, 'array': list, 'string': str}[kind]
    if not isinstance(value, expected):
        yield path + ': type'
        return
    if kind == 'object':
        properties = schema['properties']
        if any(key not in properties for key in value):
            yield path + ': additionalProperties'
        for field in schema['required']:
            if field not in value:
                yield path + '.' + field + ': required'
        for field, field_schema in properties.items():
            if field in value:
                yield from _problems(value[field], field_schema, path + '.' + field)
    elif kind == 'array':
        if not schema['minItems'] <= len(value) <= schema['maxItems']:
            yield path + ': item count'
        for index, item in enumerate(value):
            yield from _problems(item, schema['items'], f'{path}[{index}]')
    elif 'enum' in schema:
        if value not in schema['enum']:
            yield path + ': enum'
    elif not value.strip() or '\0' in value or not schema['minLength'] <= len(value) <= schema['maxLength']:
        yield path + ': nonblank string length/no NUL'


def validate_submission(operation, proposal):
    """Validate without converting prose or changing acceptance/dispatch state."""
    schema = _SCHEMAS[operation]
    try:
        encoded = canonical(proposal).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise _invalid(operation, 'proposal must be a JSON object') from None
    if len(encoded) > MAX_ENCODED_BYTES:
        raise _invalid(operation, 'encoded proposal exceeds the byte limit')
    errors = list(islice(_problems(proposal, schema), 4))
    if errors:
        raise _invalid(operation, '; '.join(errors))
