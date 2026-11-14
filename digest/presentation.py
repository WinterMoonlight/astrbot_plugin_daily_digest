"""The same factual content feeds image cards and their plain-text fallback."""


def checked_report(value, known_sources):
    if not isinstance(value, dict) or not isinstance(value.get('intro'), str):
        raise ValueError('图片内容缺少开场文字')
    sections = value.get('sections')
    if not isinstance(sections, list) or not 1 <= len(sections) <= 100:
        raise ValueError('图片内容缺少有效事项卡片')
    if len(value['intro']) > 500:
        raise ValueError('图片开场过长')
    result = {'intro': value['intro'].strip(), 'sections': []}
    for item in sections:
        if not isinstance(item, dict):
            raise ValueError('图片事项格式无效')
        for field, limit in [('label', 40), ('title', 160), ('body', 20000)]:
            if not isinstance(item.get(field), str) or not item[field].strip() or len(item[field]) > limit:
                raise ValueError('图片事项的分类、标题或正文无效')
        sources = item.get('sources')
        if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or s not in known_sources for s in sources):
            raise ValueError('图片事项缺少有效来源')
        result['sections'].append({key: item[key].strip() for key in ('label', 'title', 'body')})
    if sum(len(s['body']) for s in result['sections']) > 50000:
        raise ValueError('图片内容过长')
    return result


def report_text(report):
    parts = [report['intro']] if report['intro'] else []
    parts.extend(f"{item['title']}\n{item['body']}" for item in report['sections'])
    return '\n\n'.join(parts)
