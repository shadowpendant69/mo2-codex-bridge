"""Command-line and reusable client for an already-running MO2."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class BridgeClient:
    def __init__(self, mo2_path=None):
        if not mo2_path:
            mo2_path = os.environ.get('MO2_PATH')
        if not mo2_path:
            config = Path.home() / '.mo2-codex-bridge' / 'config.json'
            if config.exists():
                mo2_path = json.loads(config.read_text(encoding='utf-8')).get('mo2_path')
        self.mo2 = Path(mo2_path).expanduser().resolve() if mo2_path else None

    def request(self, endpoint, body=None):
        if self.mo2 is None:
            raise RuntimeError('Configure the bridge with setup.py --mo2 <MO2-folder>, or supply --mo2 / MO2_PATH')
        session_path = self.mo2 / 'plugins/data/mo2_codex_bridge/session.json'
        if not session_path.exists():
            raise RuntimeError('MO2 bridge is offline. Open MO2 with the bridge plugin installed.')
        session = json.loads(session_path.read_text(encoding='utf-8'))
        if session.get('host') != '127.0.0.1' or type(session.get('port')) is not int or not 1 <= session['port'] <= 65535:
            raise RuntimeError('Invalid local bridge session')
        request = urllib.request.Request(f"http://127.0.0.1:{session['port']}{endpoint}",
                                         data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
                                         headers={'Authorization': 'Bearer ' + session['token'],
                                                  'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(json.load(error).get('error', str(error))) from None
        except urllib.error.URLError:
            raise RuntimeError('Cannot reach MO2 bridge. Its session may be stale; restart MO2.') from None
        except TimeoutError:
            raise TimeoutError('MO2 is not answering yet; an installer may still be open') from None

    def submit(self, action, params=None, request_id=None):
        return self.request('/rpc', {'action': action, 'params': params or {},
                                     'request_id': request_id or uuid.uuid4().hex})

    def job(self, job_id):
        if not isinstance(job_id, str) or not job_id.replace('-', '').isalnum():
            raise ValueError('Invalid job ID')
        return self.request('/jobs/' + job_id)

    def call(self, action, params=None, timeout=30, request_id=None):
        receipt = self.submit(action, params, request_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                job = self.job(receipt['job_id'])
            except TimeoutError:
                break
            if job['status'] == 'done':
                return job['result']
            if job['status'] == 'failed':
                raise RuntimeError(job['error'])
            if action in {'install_archive', 'update_archive'} and job['status'] == 'running':
                self.advance_quick_install((params or {}).get('name'))
            time.sleep(0.15)
        return {'pending': True, 'job_id': receipt['job_id'],
                'message': 'Operation may still run. Query this job ID; do not submit a duplicate.'}

    def advance_quick_install(self, expected_name):
        receipt = self.submit('installer_state')
        job = self.job(receipt['job_id'])
        if job['status'] != 'done':
            return
        dialog = job['result']
        if not dialog.get('installer_open') or dialog.get('title') != 'Quick Install':
            return
        controls = dialog['controls']
        target = next((x for x in controls if x.get('name') == 'nameCombo'), None)
        button = next((x for x in controls if x.get('name') == 'okBtn' and x.get('enabled')), None)
        if target and button and target.get('value') == expected_name:
            selection = self.submit('installer_choose', {'dialog_id': dialog['dialog_id'],
                         'dialog_revision': dialog['dialog_revision'], 'control_id': button['id']})
            completed = self.job(selection['job_id'])
            if completed['status'] == 'failed':
                raise RuntimeError(completed['error'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mo2', default=None)
    parser.add_argument('action')
    parser.add_argument('--params-file')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--request-id')
    args = parser.parse_args()
    params = json.loads(Path(args.params_file).read_text(encoding='utf-8-sig')) if args.params_file else {}
    client = BridgeClient(args.mo2)
    try:
        result = client.job(params['job_id']) if args.action == 'job' else client.call(args.action, params, args.timeout, args.request_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
