"""Receipt-driven, bounded reply windows and isolated clarification history."""
from datetime import timedelta

from .domain import parse_time, utcnow
from .workflow_store import entities, entity, put


DEFAULT_WINDOW_MINUTES = 5
DEFAULT_MESSAGE_LIMIT = 3
MAX_TURNS = 12
MAX_HISTORY_CHARS = 12000
KINDS = ('list', 'reminder', 'change')


def references(scope):
    return {kind: [{'id': item['id'], **({'version': item['version']}
                  if kind == 'reminder' else {})} for item in scope.get(kind, [])]
            for kind in KINDS}


def scoped_context(record, context):
    """Resolve IDs against current business data, never against saved model output."""
    result = {'quoted': context.get('quoted', [])}
    for kind in KINDS:
        ids = {item['id'] for item in record['refs'].get(kind, [])}
        result[kind] = [item for item in context[kind] if item['id'] in ids]
    return result


def valid(record, context, now):
    scoped = scoped_context(record, context)
    for kind in KINDS:
        originals = record['refs'].get(kind, [])
        if len(scoped[kind]) != len(originals):
            return False
        versions = {item['id']: item.get('version') for item in originals}
        for item in scoped[kind]:
            if item.get('delete_pending_task'):
                return False
            if kind == 'list' and (item['state'] != 'active' or
                    not item.get('expires_at') or parse_time(item['expires_at']) <= now):
                return False
            if kind == 'reminder' and (item['version'] != versions[item['id']] or
                    item['state'] in ('cancelled', 'missed', 'acknowledged', 'native_absent') or
                    parse_time(item['event_at']) <= now):
                return False
            if kind == 'change' and item['state'] != 'pending':
                return False
    return any(scoped[kind] for kind in KINDS)


def active(record, context, now, received_at=None, *, check_limit=True):
    return (record['state'] == 'active' and record.get('expires_at') is not None and
            parse_time(record['expires_at']) > now and
            (received_at is None or parse_time(record['opened_at']) <= received_at) and
            (not check_limit or record['remaining'] > 0) and valid(record, context, now))


def queue(db, owner, channel, recipient, scope, settings, turns=None):
    """An outgoing question does not open its window until all parts are acknowledged."""
    ident = channel + ':' + owner
    if db.execute("SELECT 1 FROM entities WHERE kind='dialogue' AND id=?", (ident,)).fetchone():
        return
    turns = list(turns or [])
    if len(turns) > MAX_TURNS or sum(len(turn['content']) for turn in turns) > MAX_HISTORY_CHARS:
        raise ValueError('追问上下文过长，请重新说明完整操作')
    task_ids = set()
    for kind in KINDS:
        for item in scope.get(kind, []):
            task_ids.update(item.get('task_ids', []))
            if item.get('task_id'):
                task_ids.add(item['task_id'])
    put(db, 'dialogue', {
        'id': ident, 'recipient': recipient, 'owner': owner, 'channel': channel,
        'refs': references(scope), 'task_ids': sorted(task_ids), 'turns': turns,
        'state': 'awaiting_delivery', 'created_at': utcnow().isoformat(),
        'opened_at': None, 'expires_at': None,
        'window_minutes': settings.get('reply_window_minutes', DEFAULT_WINDOW_MINUTES),
        'remaining': settings.get('reply_message_limit', DEFAULT_MESSAGE_LIMIT),
    })


def close(db, identifiers, state='closed'):
    for ident in set(identifiers):
        record = entity(db, 'dialogue', ident)
        record['state'] = state
        record['turns'] = []
        put(db, 'dialogue', record)


def consume(db, identifiers):
    for ident in set(identifiers):
        record = entity(db, 'dialogue', ident)
        record['remaining'] = max(0, record['remaining'] - 1)
        put(db, 'dialogue', record)


def reconcile(db, now):
    contexts = {}
    for record in entities(db, 'dialogue'):
        if record['state'] not in ('awaiting_delivery', 'active'):
            continue
        changed = False
        if record['state'] == 'awaiting_delivery':
            rows = db.execute('SELECT state,ack_at,created_at FROM outbox WHERE owner=? AND channel=?',
                              (record['owner'], record['channel'])).fetchall()
            if not rows or not all(row['state'] == 'acknowledged' for row in rows):
                # Failed or interrupted questions never retain history indefinitely.
                if parse_time(record['created_at']) + timedelta(days=1) <= now:
                    close(db, [record['id']], 'expired')
                continue
            opened = max(parse_time(row['ack_at'] or row['created_at']) for row in rows)
            record['opened_at'] = opened.isoformat()
            record['expires_at'] = (opened + timedelta(minutes=record['window_minutes'])).isoformat()
            record['state'] = 'active'
            changed = True
        recipient = record['recipient']
        if recipient not in contexts:
            contexts[recipient] = {kind: entities(db, kind, recipient) for kind in KINDS}
        if parse_time(record['expires_at']) <= now or not valid(record, contexts[recipient], now):
            record['state'] = 'expired'
            record['turns'] = []
            changed = True
        if changed:
            put(db, 'dialogue', record)


def discard_task(db, task_id):
    """Erase conversational text with its task; keep a minimal stale-quote marker."""
    for record in entities(db, 'dialogue'):
        if task_id in record['task_ids']:
            record.update(state='closed', turns=[], refs={kind: [] for kind in KINDS})
            record['task_ids'] = [ident for ident in record['task_ids'] if ident != task_id]
            put(db, 'dialogue', record)
