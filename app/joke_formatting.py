"""Plain joke text plus a small, validated subset of Quill Delta formatting."""
import json
from markupsafe import Markup, escape

FORMATS = {'bold': 'strong', 'italic': 'em', 'strike': 's'}


def parse_format(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 1_000_000:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('ops'), list) or len(data['ops']) > 20000:
        return None
    ops = []
    length = 0
    for op in data['ops']:
        if not isinstance(op, dict) or not isinstance(op.get('insert'), str):
            return None  # No embeds, retain/delete instructions, images or arbitrary HTML.
        text = op['insert'].replace('\r\n', '\n').replace('\r', '\n').replace('\u00a0', ' ')
        length += len(text)
        if length > 200000:
            return None
        attrs = op.get('attributes', {})
        if not isinstance(attrs, dict):
            return None
        allowed = {key: True for key in FORMATS if attrs.get(key) is True}
        if text:
            cleaned = {'insert': text}
            if allowed:
                cleaned['attributes'] = allowed
            ops.append(cleaned)
    while ops:
        ops[0]['insert'] = ops[0]['insert'].lstrip()
        if ops[0]['insert']: break
        ops.pop(0)
    while ops:
        ops[-1]['insert'] = ops[-1]['insert'].rstrip()
        if ops[-1]['insert']: break
        ops.pop()
    return ops


def prepare_joke_body(body, formatting, actor):
    ops = parse_format(formatting)
    if ops is not None:
        return ''.join(op['insert'] for op in ops), json.dumps({'ops': ops}, ensure_ascii=False, separators=(',', ':'))
    # Existing plain text / staff HTML submissions keep their previous behavior.
    from .routes import maybe_sanitize_post_html
    return maybe_sanitize_post_html(body or '', actor), None


def render_joke_body(joke):
    body = joke.body or ''
    ops = parse_format(getattr(joke, 'body_format', None))
    if ops is not None and ''.join(op['insert'] for op in ops) == body.strip():
        fragments = []
        for op in ops:
            text = escape(op['insert'])
            for key, tag in FORMATS.items():
                if op.get('attributes', {}).get(key):
                    text = Markup('<{}>').format(tag) + text + Markup('</{}>').format(tag)
            fragments.append(text)
        return Markup('').join(fragments)
    author = getattr(joke, 'author', None)
    if author and (author.is_admin or author.is_moderator) and not getattr(joke, 'body_format', None):
        from .routes import sanitize_admin_html
        return Markup(sanitize_admin_html(body))
    return escape(body)
