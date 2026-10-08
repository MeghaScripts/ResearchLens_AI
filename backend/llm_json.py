"""
Robust JSON parsing for LLM output.

LLMs routinely return JSON that json.loads() rejects:
  * wrapped in ```json fences or surrounded by chatter
  * raw newlines/tabs inside string values  ("Unterminated string ...")
  * stray backslashes (LaTeX), trailing commas
  * output cut off by max_tokens           ("Expecting value: line N column M")

robust_json_loads() repairs all of these and only raises if nothing usable
can be recovered.
"""
import json
import re


def _strip_fences(raw):
    raw = (raw or '').strip()
    raw = re.sub(r'^```[a-zA-Z]*\s*', '', raw)
    raw = re.sub(r'\s*```\s*$', '', raw)
    return raw.strip()


def _extract_json_block(raw):
    """Cut leading/trailing chatter: keep from first { or [ onward."""
    starts = [i for i in (raw.find('{'), raw.find('[')) if i != -1]
    if not starts:
        return raw
    start = min(starts)
    return raw[start:]


def _escape_control_chars_in_strings(raw):
    """Escape literal newlines / tabs that appear inside JSON strings."""
    out, in_str, esc = [], False, False
    for ch in raw:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == '\\':
                out.append(ch)
                esc = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch == '\n':
                out.append('\\n')
            elif ch == '\r':
                out.append('\\r')
            elif ch == '\t':
                out.append('\\t')
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
    return ''.join(out)


def _close_truncated(raw):
    """Close an unterminated string and any still-open brackets/braces."""
    stack, in_str, esc = [], False, False
    for ch in raw:
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in '{[':
                stack.append('}' if ch == '{' else ']')
            elif ch in '}]' and stack:
                stack.pop()
    fixed = raw
    if in_str:
        if esc:
            fixed = fixed[:-1]
        fixed += '"'
    fixed = re.sub(r',\s*$', '', fixed.rstrip())
    # a dangling  "key":  with no value -> drop the key
    fixed = re.sub(r',?\s*"[^"\\]*"\s*:\s*$', '', fixed)
    return fixed + ''.join(reversed(stack))


def _salvage_prefix(raw):
    """Trim back to the last fully-written object/array element."""
    for m in reversed(list(re.finditer(r'[}\]]', raw))):
        candidate = _close_truncated(raw[:m.end()])
        try:
            return json.loads(candidate, strict=False)
        except Exception:
            continue
    return None


def robust_json_loads(raw):
    text = _strip_fences(raw)
    text = _extract_json_block(text)
    text = re.sub(r'\\(?!["\\/bfnrtu])', '', text)         # invalid backslashes
    text = _escape_control_chars_in_strings(text)
    text = re.sub(r',(\s*[}\]])', r'\1', text)               # trailing commas
    try:
        return json.loads(text, strict=False)
    except Exception as first_err:
        for attempt in (_close_truncated(text),):
            try:
                return json.loads(attempt, strict=False)
            except Exception:
                pass
        salvaged = _salvage_prefix(text)
        if salvaged is not None:
            return salvaged
        raise first_err