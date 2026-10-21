"""Static representation checks for generated relations, before browser execution."""
import math
import re

EXTRACT_TYPES = {
    'text': 'string', 'value': 'string', 'selected': 'string',
    'texts': 'strings', 'number': 'number', 'numeric_value': 'number',
    'count': 'number', 'numbers': 'numbers', 'numeric_values': 'numbers',
    'checked': 'boolean', 'enabled': 'boolean', 'visible': 'boolean',
    'numeric_valid': 'boolean',
}


def validate_types(relation, extractors):
    types = {name: EXTRACT_TYPES[kind] for name, kind in extractors.items()}

    def operand_type(token):
        if token in types:
            return types[token]
        if token.startswith(('"', "'")):
            return 'string'
        if token.lower() in ('true', 'false'):
            return 'boolean'
        if token.lower() == 'null':
            return 'null'
        call = re.fullmatch(r'([a-z_]+)\(([a-z][a-z0-9_]*)\)', token)
        if call:
            fn, name = call.groups()
            source = types[name]
            if fn in ('sum', 'sorted_asc', 'sorted_desc'):
                allowed = {'numbers'}
            elif fn == 'no_errors':
                allowed = {'strings'}
            elif fn in ('count', 'unique'):
                allowed = {'strings', 'numbers'}
            elif fn == 'len':
                allowed = {'string', 'strings', 'numbers'}
            else:  # product and numeric validity predicates parse numeric text.
                allowed = {'number', 'string', 'strings', 'numbers'}
            if source not in allowed:
                raise ValueError(f'{fn}({name}) cannot consume {source}; use a compatible extractor')
            return 'number' if fn in ('sum', 'product', 'count', 'len') else 'boolean'
        # Match numeric literal forms supported by Relation._resolve.
        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', token) or not math.isfinite(float(token)):
            raise ValueError('unsupported or nonfinite assertion literal')
        return 'number'

    left, right = operand_type(relation.left), operand_type(relation.right)
    if left != right:
        raise ValueError(f'incompatible comparison representations: {left} {relation.operator} {right}')
    if relation.operator not in ('==', '!=') and left != 'number':
        raise ValueError('ordered comparisons require numeric observations or reductions')
