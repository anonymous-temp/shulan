"""Bounded syntax-only recovery: escape literal whitespace inside JSON strings."""


def escape_string_controls(text, *, limit=4096):
    inside, escaped, count, output = False, False, 0, []
    replacements = {'\t': '\\t', '\r': '\\r', '\n': '\\n'}
    for char in text:
        if escaped:
            escaped = False
        elif inside and char == '\\':
            escaped = True
        elif char == '"':
            inside = not inside
        elif inside and ord(char) < 32:
            if char not in replacements or count >= limit:
                return None
            output.append(replacements[char])
            count += 1
            continue
        output.append(char)
    if not count:
        return None
    return ''.join(output), count
