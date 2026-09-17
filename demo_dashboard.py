"""Read-only, explicitly labeled sample data. Never touches SQLite or sends mail."""
import time
import mailagent as a


def response(path, query):
    now = time.time()
    draft = {'endpoint': 'https://api.example.com/v1/chat/completions', 'model': 'mail-summary-demo',
             'prompt': 'Summarize in English.\nInclude key points, action items, amounts, and deadlines.\nUse short bullets. Keep it under 150 words.',
             'model_options': ['mail-summary-demo', 'fast-summary-demo']}
    examples = [
        ('Friday product review', 'Alex <alex@example.com>', 'sent', 18.4, 'Product review: Friday, 3 PM.\n• Submit updated plans by Thursday.\n• Duration: 45 minutes.'),
        ('September invoice', 'Billing <billing@example.com>', 'sent', 12.8, 'September invoice: ¥128.\n• Payment due September 25.'),
        ('Design Weekly #42', 'Design Weekly <hello@example.com>', 'processing', None, None),
        ('Partnership follow-up', 'Sam <sam@example.com>', 'retry', None, 'Follow-up on the partnership proposal.\n• Reply with your availability for next week.'),
        ('Your order has shipped', 'Store <orders@example.com>', 'sent', 16.2, 'Order shipped. Expected arrival in 2–3 days.\nNo action required.'),
    ]
    items = [{'id': str(1884700100+i), 'sender': sender, 'subject': subject, 'received': now-180-i*330,
              'discovered': now-175-i*330, 'state': state, 'attempts': 2 if state=='retry' else 1,
              'summary': summary, 'telegram_id': 100+i if state=='sent' else None,
              'sent': now-180-i*330+latency if latency else None, 'next_try': now+25,
              'last_error': 'http_429' if state=='retry' else None, 'latency': latency}
             for i,(subject,sender,state,latency,summary) in enumerate(examples)]
    if path == '/api/overview':
        return {'counts': {'sent': 3, 'processing': 1, 'retry': 1}, 'sent_24h': 3, 'average_seconds': 15.8,
                'on_time_percent': 100, 'connections': {k:{'configured':True,'verified':True} for k in ('gmail','model','telegram')},
                'heartbeat': {'worker_ok': 2, 'collector_ok': 6}, 'model': draft['model'], 'server_time': now}
    if path == '/api/settings':
        return {'draft': draft, 'active': draft, 'api_key_configured': True, 'revision': 1, 'activated_at': now-86400}
    if path == '/api/mail':
        term, state = query.get('q', [''])[0].lower(), query.get('state', [''])[0]
        items = [r for r in items if (not state or r['state']==state) and term in (r['subject']+r['sender']+r['id']).lower()]
        return {'items': items, 'total': len(items), 'limit': 20, 'page': 0}
    if path == '/api/logs':
        events = [{'seq': 10-i, 'job_id': items[i%5]['id'], 'at':now-30-i*18, 'kind':kind,
                   'code': 'http_429' if kind=='retry' else 'read_mail' if kind=='tool' else None, 'attempt': 1}
                  for i,kind in enumerate(['sent','tool','discovered','retry','sent','attempt','discovered'])]
        if query.get('errors', ['0'])[0]=='1':
            events = [r for r in events if r['kind']=='retry']
        return {'items': events, 'total':len(events), 'limit':30, 'page':0}
    raise a.Failure('not_found')
