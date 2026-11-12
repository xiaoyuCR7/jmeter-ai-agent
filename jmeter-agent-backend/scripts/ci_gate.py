"""Run a test plan and fail the CI job when the run fails its gate.

    python scripts/ci_gate.py --plan <test_plan_id> [--url http://127.0.0.1:8000] [--timeout 7200]

The gate fails when a performance requirement fails or cannot be evaluated, or the run regressed
against the active baseline. It is an error when the run did not complete, when the machine running
JMeter was saturated (the numbers are then its own), or when the project has neither requirements
nor a baseline, so nothing was checked; --lenient accepts the last case, and a requirement that
could not be evaluated, with a warning. Exit codes: 0 pass, 1 fail, 2 error or timeout.
The access token comes from --token, the JMETER_AGENT_TOKEN variable, or the backend's
.local_access_token file. The backend only listens on this machine, so run this on the machine
that runs JMeter Agent (a self-hosted CI runner). Only the standard library is used.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.local_access_token')
EXIT = {'pass': 0, 'fail': 1}


def token(explicit: str) -> str:
    if explicit:
        return explicit
    if os.environ.get('JMETER_AGENT_TOKEN'):
        return os.environ['JMETER_AGENT_TOKEN']
    try:
        with open(TOKEN_FILE, encoding='utf-8') as stream:
            return stream.read().strip()
    except OSError:
        sys.exit('No access token: pass --token, set JMETER_AGENT_TOKEN, or start the backend once')


def call(base: str, key: str, method: str, path: str) -> dict:
    request = urllib.request.Request(base.rstrip('/') + '/api/v1' + path, method=method,
                                     headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', 'replace')
        raise RuntimeError(f'{method} {path} -> HTTP {error.code}: {detail}') from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--plan', required=True, help='Test plan ID to run')
    parser.add_argument('--url', default=os.environ.get('JMETER_AGENT_URL', 'http://127.0.0.1:8000'))
    parser.add_argument('--token', default='')
    parser.add_argument('--timeout', type=float, default=7200, help='Seconds to wait for the run and its report')
    parser.add_argument('--poll', type=float, default=10)
    parser.add_argument('--lenient', action='store_true',
                        help='Pass a run nothing was checked on, or with requirements that could not be evaluated')
    parser.add_argument('--approved-sha256', default='',
                        help='Approve a plan with scripts: the sha256 the first refusal printed')
    args = parser.parse_args(argv)
    key = token(args.token)
    query = f'?approved_sha256={args.approved_sha256}' if args.approved_sha256 else ''
    try:
        execution = call(args.url, key, 'POST', f'/test-plans/{args.plan}/execute{query}')
        print(f"Started run {execution['id']}", flush=True)
        deadline = time.monotonic() + args.timeout
        while True:
            result = call(args.url, key, 'GET',
                          f"/executions/{execution['id']}/gate" + ('?strict=false' if args.lenient else ''))
            if result.get('gate') != 'pending':
                break
            if time.monotonic() > deadline:
                print(f'Timed out after {args.timeout:g}s; the run is still {result.get("status")}', file=sys.stderr)
                return 2
            time.sleep(args.poll)
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    for reason in result.get('reasons') or []:
        print(f'FAIL: {reason}', file=sys.stderr)
    for warning in result.get('warnings') or []:
        print(f'WARN: {warning}', file=sys.stderr)
    return EXIT.get(result.get('gate'), 2)


if __name__ == '__main__':
    sys.exit(main())
