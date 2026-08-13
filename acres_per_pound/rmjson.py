"""Extraction helpers for Rightmove embedded JSON payloads.

Two payloads matter:
  - search pages: <script id="__NEXT_DATA__"> -> props.pageProps.searchResults
  - detail pages: window.__PAGE_MODEL = {...} (pointer-graph encoded)

The PAGE_MODEL object is wrapped in a graph where dict/list values are
integer indices into a nodes array; decode_graph resolves them.
"""

import json
import re

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def extract_next_data(html):
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("no __NEXT_DATA__ script on page")
    return json.loads(m.group(1))


def search_results(html):
    d = extract_next_data(html)
    return d["props"]["pageProps"]["searchResults"]


def extract_js_object(text, marker):
    """Brace-match the JSON object literal assigned to `marker`."""
    i = text.find(marker)
    if i < 0:
        return None
    i = text.find("{", i)
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return None


def decode_graph(nodes, idx, seen=frozenset()):
    if idx in seen:
        return None
    val = nodes[idx]
    seen = seen | {idx}
    if isinstance(val, dict):
        return {k: decode_graph(nodes, v, seen) for k, v in val.items()}
    if isinstance(val, list):
        return [decode_graph(nodes, i, seen) for i in val]
    return val


def page_model(html):
    """Return propertyData from a Rightmove detail page."""
    obj = extract_js_object(html, "window.__PAGE_MODEL")
    if not obj:
        raise ValueError("no PAGE_MODEL on page")
    outer = json.loads(obj)
    nodes = json.loads(outer["data"])
    root = decode_graph(nodes, 0)
    pd = root.get("propertyData")
    if not pd:
        raise ValueError("propertyData not found in PAGE_MODEL")
    return pd
