"""Translate a conservative, line-oriented BSD grep BRE subset to Python re.

POSIX character classes and a constrained backreference subset are supported
only in the C/POSIX byte locale. Other cases fail closed: BSD grep's UTF-8
character classes and backreferences are not equivalent to Python's.
Bracket ranges use Unicode code-point order (the C/UTF-8 behavior tested here).
"""
from __future__ import annotations

import re


class BREError(ValueError):
    """Invalid or unsupported basic regular expression."""


# C locale definitions, expressed as re bracket contents. Use explicit byte
# ranges, not Python Unicode shorthands or the process's mutable C locale.
_C_CLASSES = {
    'alnum': r'A-Za-z0-9',
    'alpha': r'A-Za-z',
    'blank': r'\x09\x20',
    'cntrl': r'\x00-\x1f\x7f',
    'digit': r'0-9',
    'graph': r'\x21-\x7e',
    'lower': r'a-z',
    'print': r'\x20-\x7e',
    'punct': r'\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e',
    'space': r'\x09-\x0d\x20',
    'upper': r'A-Z',
    'xdigit': r'A-Fa-f0-9',
}


def _bracket(pattern, start, byte_mode):
    """Parse a BRE bracket expression; backslash is an ordinary member here."""
    i = start + 1
    negated = i < len(pattern) and pattern[i] == '^'
    if negated:
        i += 1
    members = []
    if i < len(pattern) and pattern[i] == ']':
        members.append(']')
        i += 1
    while i < len(pattern) and pattern[i] != ']':
        c = pattern[i]
        if c == '[' and i + 1 < len(pattern) and pattern[i + 1] in ':=.':
            if pattern[i + 1] != ':':
                raise BREError('collating and equivalence elements are unsupported')
            end = pattern.find(':]', i + 2)
            if end < 0:
                raise BREError('unterminated POSIX character class')
            name = pattern[i + 2:end]
            if name not in _C_CLASSES:
                raise BREError('unknown or unsupported POSIX character class')
            if not byte_mode:
                raise BREError('POSIX character classes require C/POSIX locale')
            # A tuple cannot be confused with a literal character or '-'.
            members.append((_C_CLASSES[name],))
            i = end + 2
            continue
        members.append(c)
        i += 1
    if i == len(pattern) or not members:
        raise BREError('unterminated or empty bracket expression')
    result = []
    j = 0
    while j < len(members):
        if j + 2 < len(members) and members[j + 1] == '-':
            lo, hi = members[j], members[j + 2]
            if not (isinstance(lo, str) and isinstance(hi, str)
                    and ord(lo) < 128 and ord(hi) < 128 and ord(lo) <= ord(hi)):
                raise BREError('invalid or non-ASCII bracket range')
            result.append(re.escape(lo) + '-' + re.escape(hi))
            j += 3
        else:
            # A hyphen is literal only at the beginning or end, otherwise its
            # interpretation as an overlapping range is not portable.
            if members[j] == '-' and j not in (0, len(members) - 1):
                raise BREError('ambiguous bracket range')
            result.append(members[j][0] if isinstance(members[j], tuple)
                          else re.escape(members[j]))
            j += 1
    return '[' + ('^' if negated else '') + ''.join(result) + ']', i + 1


def _validate_backrefs(pattern, byte_mode):
    """Conservatively accept C-locale references to mandatory fixed-width groups.

    With no alternation, nesting, repetition of a referenced group or internal
    repetition/anchors, the group's span is uniquely determined by its start.
    This avoids relying on different POSIX/Python capture disambiguation rules.
    """
    groups = []
    stack = []
    has_alternation = has_nesting = has_ref = False
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == '[':
            _, i = _bracket(pattern, i, byte_mode)
            continue
        if c == '\\' and i + 1 < len(pattern):
            esc = pattern[i + 1]
            if esc == '(':
                has_nesting |= bool(stack)
                group = {'fixed': True, 'closed': False, 'quantified': False}
                groups.append(group)
                stack.append(group)
            elif esc == ')':
                if stack:
                    group = stack.pop()
                    group['closed'] = True
                    following = pattern[i + 2:i + 4]
                    group['quantified'] = (following.startswith('*')
                                           or following[:2] in (r'\+', r'\?', r'\{'))
            elif esc == '|':
                has_alternation = True
            elif esc in '123456789':
                has_ref = True
                if not byte_mode:
                    raise BREError('backreferences require C/POSIX locale')
                number = int(esc)
                if (stack or number > len(groups) or not groups[number - 1]['closed']
                        or not groups[number - 1]['fixed']
                        or groups[number - 1]['quantified']):
                    raise BREError('unsupported or invalid backreference')
            elif esc in '+?{' and stack:
                stack[-1]['fixed'] = False
            i += 2
            continue
        if c in '*^$' and stack:
            stack[-1]['fixed'] = False
        i += 1
    if has_ref and (has_alternation or has_nesting):
        raise BREError('backreferences with alternation or nesting are unsupported')


def compile_bre(pattern, *, byte_mode=False):
    """Compile supported BRE syntax or raise BREError; never invoke grep.

    In the C/POSIX locale grep operates on bytes, including UTF-8 bytes.
    """
    if '\n' in pattern or '\r' in pattern or '\0' in pattern:
        raise BREError('newline and NUL in a filter are unsupported')
    _validate_backrefs(pattern, byte_mode)
    out = []
    # Per-group branch counts; an anchor counts as a token but cannot repeat.
    branches = [0]
    repeatable = False
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == '\\':
            if i + 1 >= len(pattern):
                raise BREError('trailing backslash')
            esc = pattern[i + 1]
            if esc == '(':
                out.append('(')
                branches.append(0)
                repeatable = False
                i += 2
                continue
            if esc == ')':
                if len(branches) == 1 or not branches[-1]:
                    raise BREError('unmatched or empty group')
                branches.pop()
                branches[-1] += 1
                out.append(')')
                repeatable = True
                i += 2
                continue
            if esc == '|':
                if not branches[-1]:
                    raise BREError('empty alternative')
                out.append('|')
                branches[-1] = 0
                repeatable = False
                i += 2
                continue
            if esc in '+?{':
                if not repeatable:
                    raise BREError('repetition without an operand')
                if esc == '{':
                    end = pattern.find(r'\}', i + 2)
                    if end < 0:
                        raise BREError('unterminated repetition interval')
                    bounds = pattern[i + 2:end]
                    match = re.fullmatch(r'([0-9]+)(?:,([0-9]*))?', bounds)
                    if not match:
                        raise BREError('invalid repetition interval')
                    low_digits = match.group(1).lstrip('0') or '0'
                    high_digits = match.group(2)
                    if high_digits:
                        high_digits = high_digits.lstrip('0') or '0'
                    if len(low_digits) > 3 or (high_digits and len(high_digits) > 3):
                        raise BREError('repetition interval outside BSD grep limits')
                    low = int(low_digits)
                    high = int(high_digits) if high_digits else None
                    if low > 255 or (high is not None and (high > 255 or high < low)):
                        raise BREError('repetition interval outside BSD grep limits')
                    normalized = low_digits
                    if match.group(2) is not None:
                        normalized += ',' + (high_digits or '')
                    out.append('{' + normalized + '}')
                    i = end + 2
                else:
                    out.append(esc)
                    i += 2
                repeatable = False
                continue
            if esc in '123456789':
                # Parentheses disambiguate \1 followed by a digit and ensure
                # a following repetition applies to the complete reference.
                out.append(r'(?:\%s)' % esc)
                branches[-1] += 1
                repeatable = True
                i += 2
                continue
            # Other BSD extensions (such as word/locale classes) are
            # intentionally not translated as Python escapes.
            if esc not in r'.*[]^$\}':
                raise BREError('unsupported escaped expression: \\' + esc)
            out.append(re.escape(esc))
            branches[-1] += 1
            repeatable = True
            i += 2
            continue
        if c == '[':
            text, i = _bracket(pattern, i, byte_mode)
            out.append(text)
            branches[-1] += 1
            repeatable = True
            continue
        if c == '*':
            if repeatable:
                out.append('*')
                repeatable = False
            elif branches[-1] == 0:
                out.append(r'\*')
                branches[-1] += 1
                repeatable = True
            else:
                raise BREError('invalid repetition operator')
            i += 1
            continue
        if c == '^' and branches[-1] == 0:
            out.append('^')
            repeatable = False
        elif c == '$' and (i + 1 == len(pattern) or pattern[i + 1:i + 3] == r'\)'):
            out.append('$')
            repeatable = False
        elif c == '.':
            out.append('.')
            repeatable = True
        else:
            # In BRE, unescaped + ? ( ) { } | are literal.
            out.append(re.escape(c))
            repeatable = True
        branches[-1] += 1
        i += 1
    if len(branches) != 1:
        raise BREError('unclosed group')
    if not branches[0] and out:
        raise BREError('empty alternative')
    try:
        translated = ''.join(out)
        return re.compile(translated.encode('utf-8') if byte_mode else translated)
    except (re.error, RecursionError, OverflowError, UnicodeError) as exc:
        raise BREError('invalid expression') from exc
