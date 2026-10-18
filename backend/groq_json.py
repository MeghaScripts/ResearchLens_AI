"""
Reliable JSON from an LLM.

Why: reasoning models (e.g. openai/gpt-oss-120b) spend part of max_tokens on hidden reasoning, so the
visible JSON is often cut off ("Unterminated string starting at ...") and models sometimes emit trailing
commas, single quotes, raw newlines inside strings or text around the object ("Expecting property name
enclosed in double quotes ..."). Endpoints that did a bare json.loads() then failed with HTTP 500.

`chat_json()` asks for JSON mode, parses tolerantly, repairs/salvages, and retries with a larger token
budget and a stricter instruction. It raises json.JSONDecodeError only if every attempt fails, so existing
`except json.JSONDecodeError` handlers keep working.
"""
import json
import re

_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu]|u[0-9a-fA-F]{4})')


def _strip_wrappers(raw):
    raw = (raw or '').strip()
    raw = re.sub(r'^```[a-zA-Z]*\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    start = min([i for i in (raw.find('{'), raw.find('[')) if i >= 0], default=-1)
    if start > 0:
        raw = raw[start:]                       # drop chatter before the JSON
    return raw.strip()


def _fix_strings(raw):
    """Escape raw newlines/tabs inside string literals; drop invalid backslash escapes."""
    out, in_str, esc = [], False, False
    for ch in raw:
        if in_str:
            if esc:
                out.append(ch); esc = False
            elif ch == '\\':
                out.append(ch); esc = True
            elif ch == '"':
                out.append(ch); in_str = False
            elif ch == '\n':
                out.append('\\n')
            elif ch == '\r':
                pass
            elif ch == '\t':
                out.append('\\t')
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
    return ''.join(out)


def _light_repairs(s):
    s = _BAD_ESCAPE.sub('', s)
    s = _fix_strings(s)
    s = re.sub(r',\s*([}\]])', r'\1', s)                              # trailing commas
    s = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', s)   # unquoted keys
    s = re.sub(r'\bNone\b', 'null', s)
    s = re.sub(r'\bTrue\b', 'true', s)
    s = re.sub(r'\bFalse\b', 'false', s)
    return s


def _close_truncated(s):
    """Recover a document cut off mid-way: truncate to the last complete container (or, failing
    that, the last complete member) and close the brackets that are still open."""
    stack, in_str, esc = [], False, False
    bracket_cuts, comma_cuts = [], []
    for i, ch in enumerate(s):
        if in_str:
            if esc: esc = False
            elif ch == '\\': esc = True
            elif ch == '"': in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in '{[':
            stack.append('}' if ch == '{' else ']')
        elif ch in '}]':
            if stack: stack.pop()
            if stack: bracket_cuts.append((i + 1, list(stack)))
        elif ch == ',' and stack:
            comma_cuts.append((i, list(stack)))
    for cuts in (bracket_cuts, comma_cuts):
        for end, st in reversed(cuts):
            attempt = s[:end] + ''.join(reversed(st))
            try:
                val = json.loads(re.sub(r',\s*([}\]])', r'\1', attempt))
            except Exception:
                continue
            if isinstance(val, (dict, list)) and val:
                return val
    return None


def parse_llm_json(raw):
    """Parse model output into Python data, repairing common defects. Raises json.JSONDecodeError."""
    text = _strip_wrappers(raw)
    first_err = None
    for candidate in (text, _light_repairs(text)):
        try:
            val = json.loads(candidate)
            if isinstance(val, (dict, list)):
                return val
            first_err = first_err or json.JSONDecodeError('not a JSON object', candidate, 0)
        except json.JSONDecodeError as e:
            first_err = first_err or e
    repaired = _light_repairs(text)
    salvaged = _close_truncated(repaired)
    if salvaged is not None:
        return salvaged
    raise first_err or json.JSONDecodeError('empty response', text, 0)


def chat_json(client, model, prompt, max_tokens=2000, temperature=0.3, retries=2, label='llm', system=None):
    """Call the chat API and return parsed JSON, retrying with more tokens on failure."""
    messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': prompt}]
    budget = max(int(max_tokens), 1500)
    last = None
    use_json_mode = True
    for attempt in range(retries + 1):
        kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=budget)
        if use_json_mode:
            kwargs['response_format'] = {'type': 'json_object'}
        try:
            resp = client.chat.completions.create(**kwargs)
        except TypeError:
            use_json_mode = False
            continue
        except Exception as e:
            # JSON mode can reject a request when the model can't produce strict JSON -> retry without it
            if use_json_mode and 'json' in str(e).lower():
                use_json_mode = False
                continue
            raise
        choice = resp.choices[0]
        raw = choice.message.content or ''
        try:
            return parse_llm_json(raw)
        except json.JSONDecodeError as e:
            last = e
            print(f"[{label}] bad JSON on attempt {attempt + 1}/{retries + 1} "
                  f"(finish={getattr(choice, 'finish_reason', '?')}, {e}); retrying with more tokens")
            budget = int(budget * 1.7)
            messages = messages[:-1] + [{'role': 'user', 'content': prompt +
                                         '\n\nIMPORTANT: reply with ONE complete, valid JSON object only. Double-quote every key and string, '
                                         'no trailing commas, no comments, no text outside the JSON. Keep every string value short.'}]
    raise last or json.JSONDecodeError('no response', '', 0)
