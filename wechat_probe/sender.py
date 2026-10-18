"""One approved text action in the dedicated guest; never retry a send.

The reviewed private gate launcher supplies temporary UI accessibility. This
module does not discover or patch memory addresses. Probe approval is explicit
local input; gateway mode accepts only a command returned by /adapter/claim.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import replica
from .bridge import NoRedirect, canonical, settings
from .core import Journal, ProbeError, digest
from .isolation import require_live_admission, check_lease_still_active, read_lease
from .send_guard import ActionLedger, SendPlan, prepare_bound_send, unique_target, validate_bound_command
from .__main__ import private_run, emit


def operation_dir(run, name):
    if not re.fullmatch(r'[a-z0-9-]{1,32}', name):
        raise ProbeError('An explicit operation name is required.')
    path = run / ('send-' + name)
    if path.is_symlink() or (path.exists() and path.resolve() != path.absolute()):
        raise ProbeError('Send state must remain in the private run.')
    return path


def save(path, value):
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())


def exchange(endpoint, token, value, lease):
    check_lease_still_active(lease)
    req = urllib.request.Request(endpoint, data=canonical(value).encode(), method='POST',
        headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'})
    # Claim is NOT retried after any uncertain HTTP outcome.
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=10) as response:
            data = response.read(16385)
            if response.status != 200 or len(data) > 16384:
                raise ProbeError('Gateway command response invalid; no retry.')
            return json.loads(data)
    except (OSError, ValueError) as exc:
        raise ProbeError('Gateway command outcome unavailable; no automatic retry.') from exc


def native(request_path, phase):
    command = [str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'),
        '-NoProfile', '-NonInteractive', '-Mta', '-File', str(Path(__file__).with_name('native_sender.ps1')),
        '-RequestPath', str(request_path), '-Phase', phase]
    result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or len(result.stdout) > 8192:
        # Full native diagnostic stays private; it may include the approved body.
        (request_path.parent/('native-'+phase+'-error.txt')).write_text(result.stderr, encoding='utf-8')
        raise ProbeError('Native '+phase+' failed; no fallback or resend.')
    return json.loads(result.stdout)


def ui_step(run, operation):
    """Invoked only inside the reviewed temporary accessibility window."""
    # The gate just performed full admission before enabling accessibility.
    # Recheck that exact lease/process and fresh monitor without starting a
    # second slow system inventory inside the bounded UI operation window.
    lease = read_lease()
    check_lease_still_active(lease)
    if replica.process_binding() != lease['process']:
        raise ProbeError('Bound sender process changed.')
    folder = operation_dir(run, operation)
    request_path = folder/'request.json'
    request = json.loads(request_path.read_text('utf-8'))
    if request['process'] != lease['process'] or request['run']['account_dir'] != lease['account_dir']:
        raise ProbeError('Sender request differs from live admission.')
    if request['source'] not in ('owner-approved-probe','gateway-approved-draft'):
        raise ProbeError('Explicit send authorization source required.')
    validate_bound_command(request['command'], request['run'], lease['process'], request['contacts'],
        request['baseline'], time.time(), local_probe=request['source']=='owner-approved-probe')
    save(folder/'ui-consumed.json', {'started_at':time.time()})
    ui = native(request_path, 'stage')
    ui['evidence_hash'] = digest(canonical(ui))
    save(folder/'ui-evidence.json', ui)
    plan = prepare_bound_send(request['command'], request['run'], lease['process'], request['contacts'],
        request['baseline'], ui, time.time(), local_probe=request['source']=='owner-approved-probe')
    check_lease_still_active(lease)
    ledger = ActionLedger(run/'send-ledger.sqlite3')
    try:
        ledger.consume(plan, request['run'], lease['process'], ui, time.time())
    finally:
        ledger.close()
    save(folder/'plan.json', asdict(plan))
    # The second native call independently rechecks every UI condition and exact
    # full text immediately before Invoke. Its own attempt marker is exclusive.
    request['hwnd'] = plan.hwnd
    invoke_path = folder/'invoke.json'
    save(invoke_path, request)
    check_lease_still_active(lease)
    result = native(invoke_path, 'invoke')
    save(folder/'native-result.json', result)
    return result


def scan_receipt(db, config, baseline):
    shards, count = {}, 0
    for shard, after in baseline.items():
        rows, cursor = [], after
        while True:
            page = replica.read_shard(db, config['room_id'], shard, cursor)
            rows.extend(page)
            count += len(page)
            if count > 2000:
                raise ProbeError('Receipt scan exceeded the bound; outcome unknown.')
            if len(page) < 200:
                break
            cursor = page[-1]['local_id']
        shards[shard] = {'after':after, 'complete':True, 'rows':rows}
    return {'account_id':config['account_id'], 'room_id':config['room_id'],
        'process':config['process'], 'scanned_at':time.time(), 'shards':shards}


def unresolved_attempts(run):
    unresolved = []
    for attempt in run.glob('send-*/native-attempt.json'):
        folder = attempt.parent
        if (folder/'receipt.json').is_file():
            continue
        resolution = folder/'owner-resolution.json'
        if resolution.is_file():
            decision = json.loads(resolution.read_text('utf-8'))
            followup = operation_dir(run, decision['followup_operation'])
            request = json.loads((followup/'request.json').read_text('utf-8'))
            receipt = json.loads((followup/'receipt.json').read_text('utf-8'))
            original = json.loads((folder/'request.json').read_text('utf-8'))
            old = original['command']
            if (request.get('supersedes') == folder.name.removeprefix('send-')
                    and decision.get('owner_authorized_followup') is True
                    and receipt['body_sha256'] == digest(old['text'])
                    and receipt['account_id'] == old['account_id'] and receipt['room_id'] == old['room_id']):
                continue
        unresolved.append(folder.name.removeprefix('send-'))
    return unresolved


def send_once(args, run):
    lease = require_live_admission()
    journal = Journal(run/'probe.sqlite3')
    try:
        config = journal.config()
    finally:
        journal.close()
    if (config['process'] != lease['process'] or config['account_dir'] != lease['account_dir']
            or config['candidate_commit'] != replica.COMMIT or config['client_version'] != replica.CLIENT_VERSION):
        raise ProbeError('Sender read binding changed.')
    module = replica.load_db_module()
    db = replica.open_reader(module, Path(config['account_dir']), config['process'], run/'cache')
    contacts = replica.contacts(db, 'test-group')
    unique_target(contacts, 'test-group', config['room_id'])
    baseline = {rel:replica.read_shard(db, config['room_id'], rel, None)
                for rel, _, _ in db._db_files if rel.startswith('message/')}
    folder = operation_dir(run, args.operation)
    unresolved = unresolved_attempts(run)
    supersedes = getattr(args,'supersedes',None)
    if unresolved:
        if not (args.command == 'probe' and unresolved == [supersedes] and args.native_action=='click'):
            raise ProbeError('A previous desktop action has no confirmed receipt; sending stopped.')
        old = json.loads((operation_dir(run,supersedes)/'request.json').read_text('utf-8'))
        if (old['source']!='owner-approved-probe' or old['command']['text']!=args.approved_text
                or old['command']['account_id']!=config['account_id'] or old['command']['room_id']!=config['room_id']):
            raise ProbeError('Owner-authorized follow-up differs from the unresolved probe.')
    elif supersedes is not None:
        raise ProbeError('There is no matching unresolved probe to follow up.')
    folder.mkdir()  # An operation can never be rerun, even before UI input.
    receipt, command, attempted = None, None, False
    data = token = None
    if args.command == 'probe':
        text = args.approved_text
        if not 1 <= len(text) <= 800 or not text.strip() or any(ord(c) < 32 for c in text):
            raise ProbeError('An explicitly approved single-line body is required.')
        now = time.time()
        command = {'draft_id':'probe_'+digest(canonical([args.run,args.operation,text])),
            'account_id':config['account_id'], 'room_id':config['room_id'], 'text':text,
            'content_hash':digest(text), 'attempted_at':now, 'execute_before':now+120}
        source = 'owner-approved-probe'
    else:
        data, token = settings(run, config)
        save(folder/'claim-attempted.json', {'at':time.time()})
        command = exchange(data['endpoint'].removesuffix('/events')+'/claim', token, {}, lease).get('command')
        if command is None:
            return {'status':'no_approved_draft','input_or_send_performed':False}
        source = 'gateway-approved-draft'
    save(folder/'request.json', {'source':source, 'command':command, 'run':config,
        'process':lease['process'], 'contacts':contacts, 'baseline':baseline, 'hwnd':0,
        'native_action':args.native_action, 'allow_existing':bool(supersedes), 'supersedes':supersedes})
    try:
        # The launcher has its own bounded worker and finally restoration. Do
        # not kill it with an outer timeout while it owns the temporary byte.
        gate = Path.home()/'WeChatLab/setup/guest-accessibility-window.py'
        result = subprocess.run([sys.executable,str(gate),'--approved-operation',args.operation,
            '--mode','send','--run',args.run],capture_output=True,encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        outcomes = [x for x in lines if x.get('operation') == args.operation]
        if result.returncode or len(outcomes)!=1 or outcomes[0].get('restore_verified') is not True:
            raise ProbeError('Accessibility window did not complete; no resend.')
        plan_data = json.loads((folder/'plan.json').read_text('utf-8'))
        plan_data['baseline'] = tuple(tuple(x) for x in plan_data['baseline'])
        plan = SendPlan(**plan_data)
        attempted = (folder/'native-attempt.json').exists()
        ledger = ActionLedger(run/'send-ledger.sqlite3')
        try:
            deadline = time.monotonic()+30
            while time.monotonic()<deadline:
                check_lease_still_active(lease)
                scan = scan_receipt(db, config, baseline)
                try:
                    receipt = ledger.confirm_receipt(plan, scan, time.time())
                    break
                except ProbeError:
                    time.sleep(1)
        finally:
            ledger.close()
        if receipt is None:
            raise ProbeError('No unique bound-room receipt; outcome unknown and no resend.')
        save(folder/'receipt.json', receipt)
        if supersedes:
            save(operation_dir(run,supersedes)/'owner-resolution.json',
                {'owner_authorized_followup':True,'followup_operation':args.operation,
                 'original_action_remains_unconfirmed':True,'confirmed_at':time.time()})
    except BaseException as exc:
        save(folder/'failure.json', {'error_type':type(exc).__name__,
            'native_attempt_marker':(folder/'native-attempt.json').exists(), 'no_resend':True})
        raise
    finally:
        if source == 'gateway-approved-draft':
            # A missing receipt records uncertainty, never permission to retry.
            result = exchange(data['endpoint'].removesuffix('/events')+'/complete', token,
                {'draft_id':command['draft_id'],'receipt':receipt}, lease)
            save(folder/'gateway-completion.json', result)
    return {'status':'receipt_confirmed', 'source':source, 'invoke_attempted':attempted,
        'native_action':args.native_action, 'same_account_room_and_body':True,
        'receiver_confirmation_pending':True, 'automatic_resend':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('probe','gateway','ui'):
        p = sub.add_parser(name)
        p.add_argument('--operation', required=True)
        if name == 'probe':
            p.add_argument('--approved-text', required=True)
            p.add_argument('--supersedes', help='Only after explicit owner approval of one follow-up to an unknown probe.')
        if name != 'ui':
            p.add_argument('--native-action', choices=('invoke','click'),default='invoke')
    args = parser.parse_args()
    try:
        run = private_run(args.run)
        result = ui_step(run,args.operation) if args.command=='ui' else send_once(args,run)
        emit(result)
        return 0
    except Exception as exc:
        emit({'status':'stopped','error_type':type(exc).__name__,
            'reason':str(exc) if isinstance(exc,ProbeError) else 'Sender stopped; inspect private evidence.',
            'automatic_resend':False})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
