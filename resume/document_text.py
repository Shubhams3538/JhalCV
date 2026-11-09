"""Read document text with hyperlink targets kept beside their visible labels."""
from xml.etree import ElementTree as ET
import re
from .resume_import import web_links


def xml_tree(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8-sig')
    # Poppler emits a harmless external DOCTYPE. ElementTree does not retrieve
    # it; entity declarations and internal DTDs are never accepted.
    if '<!ENTITY' in value.upper() or '<!DOCTYPE' in value.upper() and '[' in value.split('<!DOCTYPE', 1)[-1].split('>', 1)[0]:
        raise ValueError('Unsupported document XML declarations.')
    return ET.fromstring(value)


def linked_text(node, relationships=None):
    tag = node.tag.rsplit('}', 1)[-1]
    if tag in ('instrText', 'del'):
        return ''
    out = node.text or ''
    if tag not in ('t', 'a', 'text', 'b', 'i') and not out.strip():
        out = ''
    for child in node:
        value = linked_text(child, relationships)
        tag = child.tag.rsplit('}', 1)[-1]
        target = child.get('href') if tag == 'a' else None
        if tag == 'hyperlink' and relationships is not None:
            target = relationships.get(child.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'))
        urls = web_links('', [target]) if target and target.startswith(('https://', 'http://')) else []
        if urls and urls[0] not in value:
            value += ' [' + urls[0] + ']'
        if tag in ('tab', 'br'):
            value += '\t' if tag == 'tab' else '\n'
        tail = child.tail or ''
        out += value + (tail if relationships is None or tail.strip() else '')
    return out


def docx_text(tree, rels=None):
    relationships = {}
    if rels is not None:
        for node in rels:
            if node.get('Type', '').endswith('/hyperlink') and node.get('TargetMode') == 'External':
                target = node.get('Target', '')
                if target.startswith(('http://', 'https://')):
                    relationships[node.get('Id')] = target
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    paragraphs = []
    for p in tree.findall('.//w:p', ns):
        value = linked_text(p, relationships)
        instructions = ' '.join([n.text or '' for n in p.findall('.//w:instrText', ns)] +
                                [n.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}instr', '') for n in p.findall('.//w:fldSimple', ns)])
        for target in re.findall(r'\bHYPERLINK\s+"([^"\n]+)"', instructions, re.I):
            for url in web_links('', [target]) if target.startswith(('http://', 'https://')) else []:
                if url not in value:
                    value += ' [' + url + ']'
        paragraphs.append(value)
    return '\n'.join(paragraphs)


def pdf_xml_text(value):
    """Use page coordinates to keep each linked label on its original row."""
    tree = xml_tree(value)
    pages = []
    for page in tree.findall('page'):
        rows = []
        nodes = page.findall('text')
        left_edge = min((float(node.get('left', 0)) for node in nodes), default=0)
        for node in sorted(nodes, key=lambda n: (float(n.get('top', 0)), float(n.get('left', 0)))):
            top = float(node.get('top', 0))
            if not rows or abs(rows[-1][0] - top) > 3:
                rows.append((top, []))
            rows[-1][1].append((float(node.get('left', 0)), linked_text(node)))
        lines = []
        for _, parts in rows:
            parts = sorted(parts)
            # Keep the relative left margin: the local parser uses it to
            # distinguish wrapped bullet text from a new employer/project.
            indent = ' ' * min(1000, max(0, round((parts[0][0] - left_edge) / 2)))
            values = [value for _, value in parts]
            if values and re.fullmatch(r'[•●▪◦*-]', values[0].strip()) and len(values) > 1:
                line = values[0] + ' ' + '    '.join(values[1:])
            else:
                line = '    '.join(values)
            lines.append(indent + line.rstrip())
        pages.append('\n'.join(lines))
    return '\n\n'.join(pages).strip()
