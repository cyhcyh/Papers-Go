"""Run inside the app container; never prints credentials."""
import json
import sys
import time
import urllib.request
import urllib.error

import jwt
from app.auth import secret
from app.db import one

admin = one('SELECT id FROM users WHERE is_admin=1 AND disabled=0 ORDER BY id LIMIT 1')
issued = int(time.time())
token = jwt.encode({'sub': str(admin['id']), 'type': 'access', 'iat': issued,
                    'exp': issued + 300, 'jti': 'classification-maintenance'}, secret(), algorithm='HS256')


def api(path, method='GET'):
    request = urllib.request.Request('http://localhost:8000/api/admin/' + path,
        data=b'{}' if method == 'POST' else None, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    for attempt in range(20):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError:
            if attempt == 19:
                raise
            time.sleep(.25)


action = sys.argv[1] if len(sys.argv) > 1 else 'status'
if action == 'stop':
    print(json.dumps(api('jobs/classify/stop', 'POST')))
elif action == 'stop_all':
    print(json.dumps(api('jobs/pipeline/stop', 'POST')))
elif action == 'start':
    print(json.dumps(api('jobs/classify', 'POST')))
print(json.dumps({'jobs': api('jobs'),
    'classify': next((s for s in api('sources') if s['name'] == 'classify'), None)}, ensure_ascii=True))
