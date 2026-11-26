"""Inspect this project's SQL templates without changing literals or parameters."""

import ast
import re


# These are SQL syntax, built-in functions, and the existing server setting.
# Tables, columns, and aliases are deliberately not included here.
SQL_SYNTAX = set('''
SELECT DISTINCT FROM WHERE AND OR ORDER BY DESC ASC LIMIT INSERT INTO VALUES
UPDATE SET DELETE IS NULL NOT IN ON DUPLICATE KEY TRUE FALSE JOIN MAX NOW
DESCRIBE AS FOREIGN_KEY_CHECKS
'''.split())
WORD = re.compile(r'[^\W\d]\w*', re.UNICODE)


def identifier_spans(sql):
    """Yield bare identifier offsets; skip quoted names, literals and f-string slots."""
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in "`'\"":
            quote = char
            index += 1
            while index < len(sql):
                if sql[index] == '\\':
                    index += 2
                elif sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
        elif char == '{':
            depth = 1
            index += 1
            while index < len(sql) and depth:
                if sql[index] == '{':
                    depth += 1
                elif sql[index] == '}':
                    depth -= 1
                index += 1
        elif sql.startswith('%s', index):
            index += 2
        else:
            match = WORD.match(sql, index)
            if match:
                if match.group().upper() not in SQL_SYNTAX:
                    yield match.start(), match.end(), match.group()
                index = match.end()
            else:
                index += 1


def quote_identifiers(sql):
    for start, end, word in reversed(list(identifier_spans(sql))):
        sql = sql[:start] + '`' + word + '`' + sql[end:]
    return sql


def sql_nodes(tree):
    seen = set()
    for node in ast.walk(tree):
        value = None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('execute', 'executemany') and node.args):
            value = node.args[0]
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in ('query', 'sql') for target in node.targets):
            value = node.value
        if isinstance(value, (ast.Constant, ast.JoinedStr)) and id(value) not in seen:
            if isinstance(value, ast.Constant) and not isinstance(value.value, str):
                continue
            seen.add(id(value))
            yield value


def sql_template(node):
    if isinstance(node, ast.Constant):
        return node.value
    return ''.join(part.value if isinstance(part, ast.Constant) else '{dynamic}' for part in node.values)
