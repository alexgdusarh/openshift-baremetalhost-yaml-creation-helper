#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/readme_to_adf.py

Converts README.md into an Atlassian Document Format (ADF) JSON document,
so it can be imported into Confluence as a real page rather than a code
block dump. See README.md's Confluence import footnote for the actual
REST API call that consumes this file's output.

This is a real (if scoped) markdown parser, not a hand-transcribed JSON
file - hand-transcribing ~470 lines of markdown into nested ADF JSON by
hand would be extremely error-prone and impossible to keep in sync as
the README changes. Re-run this script any time README.md is edited:

    python3 tools/readme_to_adf.py README.md > README.adf.json

Supported markdown, matching what README.md actually uses:
  - headings (#, ##, ###, ...)
  - paragraphs, with inline **bold**, `code`, and [text](url) links
  - fenced code blocks (```lang ... ```)
  - GitHub-style pipe tables (first row = header, second row = separator)
  - bullet lists (- item)
  - horizontal rules (--- on its own line)

Anything else (nested lists, numbered lists, images, blockquotes) isn't
used in README.md and isn't handled - extend INLINE_PATTERN / the block
parser below if a future README needs one of those.
"""

import json
import re
import sys

INLINE_PATTERN = re.compile(
    r'(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))'
)


def parse_inline(text):
    """Turn one line/paragraph of markdown into a list of ADF text nodes
    with marks (strong/code/link) applied where matched."""
    if not text:
        return []
    nodes = []
    for part in INLINE_PATTERN.split(text):
        if not part:
            continue
        if part.startswith('**') and part.endswith('**'):
            nodes.append({'type': 'text', 'text': part[2:-2], 'marks': [{'type': 'strong'}]})
        elif part.startswith('`') and part.endswith('`'):
            nodes.append({'type': 'text', 'text': part[1:-1], 'marks': [{'type': 'code'}]})
        elif part.startswith('[') and ')' in part:
            m = re.match(r'\[([^\]]+)\]\(([^)]+)\)', part)
            if m:
                nodes.append({
                    'type': 'text', 'text': m.group(1),
                    'marks': [{'type': 'link', 'attrs': {'href': m.group(2)}}],
                })
            else:
                nodes.append({'type': 'text', 'text': part})
        else:
            nodes.append({'type': 'text', 'text': part})
    return nodes


def paragraph(text):
    content = parse_inline(text)
    if not content:
        return None
    return {'type': 'paragraph', 'content': content}


def parse_table(lines):
    """lines: consecutive '|...|' lines, including the '|---|---|'
    separator as lines[1]. Returns an ADF table node."""
    def split_row(line):
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        return cells

    header_cells = split_row(lines[0])
    body_lines = lines[2:]  # skip the header and the --- separator

    rows = []
    header_row = {'type': 'tableRow', 'content': [
        {'type': 'tableHeader', 'attrs': {}, 'content': [paragraph(c) or {'type': 'paragraph', 'content': []}]}
        for c in header_cells
    ]}
    rows.append(header_row)

    for line in body_lines:
        cells = split_row(line)
        row = {'type': 'tableRow', 'content': [
            {'type': 'tableCell', 'attrs': {}, 'content': [paragraph(c) or {'type': 'paragraph', 'content': []}]}
            for c in cells
        ]}
        rows.append(row)

    return {
        'type': 'table',
        'attrs': {'isNumberColumnEnabled': False, 'layout': 'default'},
        'content': rows,
    }


def convert(markdown_text):
    lines = markdown_text.split('\n')
    content = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code block (may be indented, e.g. nested under a
        # numbered list item - strip before checking, or the fence is
        # missed entirely and its ``` markers leak into paragraph text)
        if stripped.startswith('```'):
            lang = stripped[3:].strip() or None
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            # de-indent: drop the same leading whitespace the opening
            # fence had, so code nested under a list item doesn't keep
            # that indentation inside the codeBlock's text
            indent = len(line) - len(line.lstrip())
            dedented = [l[indent:] if l[:indent].strip() == '' else l.lstrip() for l in code_lines]
            node = {'type': 'codeBlock', 'content': [{'type': 'text', 'text': '\n'.join(dedented)}]}
            if lang:
                node['attrs'] = {'language': lang}
            content.append(node)
            continue

        # heading
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            level = len(m.group(1))
            content.append({
                'type': 'heading',
                'attrs': {'level': level},
                'content': parse_inline(m.group(2).strip()),
            })
            i += 1
            continue

        # table (header row + --- separator)
        if line.strip().startswith('|') and i + 1 < n and re.match(r'^\s*\|[\s:|-]+\|\s*$', lines[i + 1] or ''):
            table_lines = [line]
            j = i + 1
            while j < n and lines[j].strip().startswith('|'):
                table_lines.append(lines[j])
                j += 1
            content.append(parse_table(table_lines))
            i = j
            continue

        # bullet list
        if re.match(r'^[-*]\s+', line.strip()):
            items = []
            while i < n and re.match(r'^[-*]\s+', lines[i].strip()):
                item_text = re.sub(r'^[-*]\s+', '', lines[i].strip())
                items.append({'type': 'listItem', 'content': [paragraph(item_text)]})
                i += 1
            content.append({'type': 'bulletList', 'content': items})
            continue

        # numbered list (e.g. "1. Edit ..." in Setup steps)
        if re.match(r'^\d+\.\s+', line.strip()):
            items = []
            while i < n and re.match(r'^\d+\.\s+', lines[i].strip()):
                item_text = re.sub(r'^\d+\.\s+', '', lines[i].strip())
                i += 1
                # a numbered item may continue on following indented
                # lines (plain text or a nested fenced code block) until
                # the next numbered item, a blank line, or EOF. Plain
                # continuation lines are joined into one flowing
                # paragraph rather than one paragraph per physical line.
                item_content = []
                para_lines = [item_text] if item_text else []
                while i < n and lines[i].strip() and not re.match(r'^\d+\.\s+', lines[i].strip()):
                    if lines[i].strip().startswith('```'):
                        if para_lines:
                            p = paragraph(' '.join(para_lines))
                            if p:
                                item_content.append(p)
                            para_lines = []
                        sub_lang = lines[i].strip()[3:].strip() or None
                        i += 1
                        code_lines = []
                        while i < n and not lines[i].strip().startswith('```'):
                            code_lines.append(lines[i].strip())
                            i += 1
                        i += 1
                        node = {'type': 'codeBlock', 'content': [{'type': 'text', 'text': '\n'.join(code_lines)}]}
                        if sub_lang:
                            node['attrs'] = {'language': sub_lang}
                        item_content.append(node)
                    else:
                        para_lines.append(lines[i].strip())
                        i += 1
                if para_lines:
                    p = paragraph(' '.join(para_lines))
                    if p:
                        item_content.append(p)
                items.append({'type': 'listItem', 'content': item_content or [{'type': 'paragraph', 'content': []}]})
            content.append({'type': 'orderedList', 'content': items})
            continue

        # horizontal rule
        if re.match(r'^-{3,}\s*$', line.strip()):
            content.append({'type': 'rule'})
            i += 1
            continue

        # blank line
        if not line.strip():
            i += 1
            continue

        # paragraph: consume until a blank line or a line starting a new block
        para_lines = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r'^(#{1,6})\s|^```|^[-*]\s+|^\d+\.\s+|^-{3,}\s*$', lines[i].strip()
        ):
            para_lines.append(lines[i])
            i += 1
        p = paragraph(' '.join(l.strip() for l in para_lines))
        if p:
            content.append(p)

    return {'type': 'doc', 'version': 1, 'content': content}


def main():
    if len(sys.argv) != 2:
        print('Usage: readme_to_adf.py <path-to-README.md>', file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        text = f.read()
    adf = convert(text)
    print(json.dumps(adf, indent=2))


if __name__ == '__main__':
    main()
