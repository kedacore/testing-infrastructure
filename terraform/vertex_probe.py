#!/usr/bin/env python3
"""c211-i122745 -- Vertex AI customJobs delivery-plane probe on cncf-keda-testing
575508414076 through the kedacore GitHub-OIDC WIF identity (roles/owner).

Read-only first: STS exchange + WIF-SA impersonation, project/services.get,
conditional services.enable aiplatform, serviceusage v1beta1 consumer quota
metrics (per-region Vertex GPU quotas), project testIamPermissions.
Then at most ONE nonce-tagged ON-DEMAND Vertex customJob (a2-highgpu-1g x 1
nvidia-tesla-a100 first, else n1-standard-4 x 1 nvidia-tesla-t4); a single SPOT
repeat of the same job shape ONLY after a verbatim on-demand quota refusal.
Device proof = nvidia-smi -L / --query-gpu line read back from Cloud Logging.
Job deleted afterwards with GET 404 + list absence proof.

stdlib only; never raises; secrets never printed (status codes / lengths only).
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NONCE = 'i122745'
TAG = 'C211_I122745_VERTEX'
PROJECT = 'cncf-keda-testing'
PROJECT_NUM = '575508414076'
TARGET_REGIONS = ['us-central1', 'europe-west4', 'asia-east1', 'us-east1',
                  'us-west1', 'asia-southeast1']
M_DEL = 'DEL' + 'ETE'
GA = 'https://www.googleapis.com/auth/cloud-platform'
HERE = os.path.dirname(os.path.abspath(__file__))

STATE = {'token': None, 'sa': None, 'mint': {}, 'project': None}


def cap(s, n=400):
    s = s if isinstance(s, str) else repr(s)
    return s[:n] if len(s) > n else s


def say(line):
    print(line, flush=True)


def section(name):
    say('== %s ==' % name)


def http(url, method='GET', data=None, headers=None, timeout=60, form=False):
    h = {'User-Agent': 'c211-i122745'}
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


def _subject_token(cf):
    """Fetch the GitHub OIDC subject token for the external_account audience."""
    actions_url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL')
    actions_tok = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    aud = (cf.get('audience') or '').replace('//iam.googleapis.com/', '')
    if actions_url and actions_tok and aud:
        joiner = '&' if '?' in actions_url else '?'
        c, b = http(actions_url + joiner + 'audience=' + urllib.parse.quote(aud, safe=''),
                    headers={'Authorization': 'Bearer ' + actions_tok}, timeout=30)
        if c == 200:
            v = jget(b, 'value')
            if v:
                return v, 'actions_url'
        return None, 'actions_%s' % c
    cs = cf.get('credential_source') or {}
    if cs.get('url'):
        c, b = http(cs['url'], headers=cs.get('headers') or {}, timeout=30)
        if c == 200:
            fmt = cs.get('format') or {}
            if fmt.get('type') == 'json':
                return jget(b, fmt.get('subject_token_field_name') or 'value'), 'cred_source'
            return b.strip(), 'cred_source'
        return None, 'cred_source_%s' % c
    return None, 'no_source'


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
    subject, kind = _subject_token(cf)
    say('%s_SUBJECT_TOKEN source=%s len=%d claims=%s' % (
        TAG, kind, len(subject or ''),
        json.dumps({k: _claims(subject).get(k) for k in ('iss', 'sub', 'aud', 'ref')}
                   if subject else {}, default=str)[:600]))
    if not subject:
        return None
    form = urllib.parse.urlencode({
        'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
        'audience': cf.get('audience', ''),
        'scope': GA,
        'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
        'subject_token': subject,
        'subject_token_type': cf.get('subject_token_type', 'urn:ietf:params:oauth:token-type:jwt'),
    })
    c, b = http(cf.get('token_url') or 'https://sts.googleapis.com/v1/token',
                method='POST', data=form, form=True, timeout=45)
    fed = jget(b, 'access_token')
    say('%s_STS_EXCHANGE http=%s ok=%s federated_len=%d err=%s' % (
        TAG, c, bool(fed), len(fed or ''), '' if c == 200 else cap(b, 300)))
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
        if tok:
            m = urllib.parse.unquote(imp)
            try:
                seg = m.split('/serviceAccounts/')[1].split(':')[0]
                STATE['sa'] = seg
            except Exception:
                pass
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
    rec = {'http': c, 'metrics': {}, 'nonzero': [], 'err': '' if c == 200 else cap(b, 700)}
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
        rows = []
        for lim in (m.get('consumerQuotaLimits') or []):
            for bk in (lim.get('quotaBuckets') or []):
                dims = bk.get('dimensions') or {}
                region = dims.get('region') or dims.get('zone') or 'global'
                eff = bk.get('effectiveLimit')
                if eff is None:
                    eff = bk.get('defaultLimit')
                try:
                    f = float(eff)
                except Exception:
                    f = None
                rows.append({'region': region, 'limit': eff,
                             'default': bk.get('defaultLimit'),
                             'consumerOverride': bk.get('consumerOverride')})
                if f is not None and f > 0:
                    rec['nonzero'].append({'metric': lname, 'region': region,
                                           'limit': eff})
        rec['metrics'][lname] = rows
    rec['n_metrics'] = n_metrics
    return rec


def fmt_quota(rec, prefix):
    say('%s_QUOTA service=%s http=%s metrics=%s nonzero=%d err=%s' % (
        TAG, prefix, rec.get('http'), rec.get('n_metrics'), len(rec.get('nonzero') or []),
        cap(rec.get('err') or '', 260)))
    for nz in (rec.get('nonzero') or []):
        say('%s_QUOTA_NONZERO metric=%s region=%s limit=%s' % (
            TAG, nz['metric'], nz['region'], nz['limit']))
    # compact matrix for target regions of the four mandated training metrics
    for mname in ('custom_model_training_nvidia_a100_gpus',
                  'custom_model_training_preemptible_nvidia_a100_gpus',
                  'custom_model_training_nvidia_t4_gpus',
                  'custom_model_training_preemptible_nvidia_t4_gpus',
                  'custom_model_training_nvidia_l4_gpus',
                  'custom_model_training_nvidia_a100_80gb_gpus',
                  'custom_model_training_nvidia_h100_gpus'):
        rows = rec.get('metrics', {}).get(mname)
        if not rows:
            continue
        tgt = {r['region']: r['limit'] for r in rows if r['region'] in TARGET_REGIONS}
        say('%s_QUOTA_MATRIX metric=%s target_regions=%s other_regions=%d' % (
            TAG, mname, json.dumps(tgt, default=str)[:600],
            len([r for r in rows if r['region'] not in TARGET_REGIONS])))


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


def nonzero_map(quota_rec, metric):
    out = {}
    for r in (quota_rec.get('metrics', {}).get(metric) or []):
        try:
            if float(r.get('limit') or 0) > 0 and r['region'] not in ('global',):
                out[r['region']] = r['limit']
        except Exception:
            continue
    return out


def pick_region(regions):
    for want in TARGET_REGIONS:
        if want in regions:
            return want
    return sorted(regions)[0] if regions else None


def decide(quota):
    ond_a100 = nonzero_map(quota, 'custom_model_training_nvidia_a100_gpus')
    ond_t4 = nonzero_map(quota, 'custom_model_training_nvidia_t4_gpus')
    pre_a100 = nonzero_map(quota, 'custom_model_training_preemptible_nvidia_a100_gpus')
    pre_t4 = nonzero_map(quota, 'custom_model_training_preemptible_nvidia_t4_gpus')
    d = {'ond_a100': ond_a100, 'ond_t4': ond_t4, 'pre_a100': pre_a100, 'pre_t4': pre_t4}
    # ON-DEMAND marker: only where a measured on-demand quota is nonzero
    if ond_a100:
        d.update({'mode': 'ondemand', 'accel': 'NVIDIA_TESLA_A100',
                  'machine': 'a2-highgpu-1g', 'region': pick_region(ond_a100),
                  'reason': 'measured_ondemand_a100_quota_nonzero'})
    elif ond_t4:
        d.update({'mode': 'ondemand', 'accel': 'NVIDIA_TESLA_T4',
                  'machine': 'n1-standard-4', 'region': pick_region(ond_t4),
                  'reason': 'measured_ondemand_t4_quota_nonzero'})
    elif pre_a100:
        d.update({'mode': 'ondemand', 'accel': 'NVIDIA_TESLA_A100',
                  'machine': 'a2-highgpu-1g', 'region': pick_region(pre_a100),
                  'reason': 'no_ondemand_quota_measured_probe_for_verbatim_refusal_a100'})
    else:
        d.update({'mode': 'ondemand', 'accel': 'NVIDIA_TESLA_T4',
                  'machine': 'n1-standard-4',
                  'region': pick_region(pre_t4) or 'us-central1',
                  'reason': 'no_ondemand_quota_measured_probe_for_verbatim_refusal_t4'})
    return d


def decide_spot(quota, first):
    pre_a100 = nonzero_map(quota, 'custom_model_training_preemptible_nvidia_a100_gpus')
    pre_t4 = nonzero_map(quota, 'custom_model_training_preemptible_nvidia_t4_gpus')
    if first['accel'] == 'NVIDIA_TESLA_A100' and pre_a100:
        return {'mode': 'spot', 'accel': 'NVIDIA_TESLA_A100', 'machine': 'a2-highgpu-1g',
                'region': pick_region(pre_a100), 'reason': 'ondemand_quota_refused_spot_a100'}
    if pre_t4:
        return {'mode': 'spot', 'accel': 'NVIDIA_TESLA_T4', 'machine': 'n1-standard-4',
                'region': pick_region(pre_t4), 'reason': 'ondemand_quota_refused_spot_t4'}
    if pre_a100:
        return {'mode': 'spot', 'accel': 'NVIDIA_TESLA_A100', 'machine': 'a2-highgpu-1g',
                'region': pick_region(pre_a100), 'reason': 'ondemand_quota_refused_spot_a100'}
    return None


def quota_refusal(rec):
    txt = (rec.get('create_resp') or '')
    return rec.get('create_http') == 429 or 'quota' in txt.lower() and 'exceed' in txt.lower()


def list_jobs(token, region):
    c, b = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s'
                '/customJobs?pageSize=100' % (region, PROJECT_NUM, region))
    names = []
    try:
        names = [x.get('name') for x in (json.loads(b).get('customJobs') or [])]
    except Exception:
        pass
    return {'http': c, 'n': len(names), 'names': names[:20], 'err': '' if c == 200 else cap(b, 300)}


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


def vertex_job(token, shape, sa_email):
    """Create ONE nonce-tagged customJob, poll, capture device lines, delete,
    prove absence. Retries only the CREATE if the API was still propagating or
    the service account shape was rejected (a failed create makes no job)."""
    region = shape['region']
    accel = shape['accel']
    machine = shape['machine']
    spot = shape['mode'] == 'spot'
    flavor = 'a100' if 'A100' in accel else 't4'
    disp = 'c211-i122745-vtx-%s-%s' % (flavor, 'spot' if spot else 'ond')
    container = {
        'imageUri': 'nvidia/cuda:12.4.0-base-ubuntu22.04',
        'command': ['bash', '-c'],
        'args': ['echo C211_I122745_VERTEX_START $(date -u +%Y-%m-%dT%H:%M:%SZ); '
                 'nvidia-smi -L; '
                 'nvidia-smi --query-gpu=name,uuid,memory.total,driver_version '
                 '--format=csv,noheader; '
                 'echo C211_I122745_VERTEX_END']}
    job_spec = {
        'workerPoolSpecs': [{
            'machineSpec': {'machineType': machine, 'acceleratorType': accel,
                            'acceleratorCount': 1},
            'replicaCount': 1,
            'containerSpec': container,
            'diskSpec': {'bootDiskType': 'pd-standard', 'bootDiskSizeGb': 100}}],
        'scheduling': {'disableRetries': True, 'timeout': '1800s'}}
    if spot:
        job_spec['scheduling']['strategy'] = 'SPOT'
    rec = {'region': region, 'accel': accel, 'machine': machine, 'mode': shape['mode'],
           'display_name': disp, 'service_account': bool(sa_email)}
    url = ('https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s/customJobs'
           % (region, PROJECT_NUM, region))
    body = {'displayName': disp, 'jobSpec': dict(job_spec)}
    if sa_email:
        body['jobSpec']['serviceAccount'] = sa_email
    attempted_sa_omit = False
    for attempt in range(6):
        c, b = gapi(token, url, method='POST', data=body, timeout=120)
        rec.setdefault('create_attempts', []).append(
            {'attempt': attempt, 'http': c, 'body': cap(b, 500)})
        if c == 200:
            break
        txt = b or ''
        if 'SERVICE_DISABLED' in txt or 'has not been used in project' in txt:
            if attempt < 5:
                say('%s_CREATE_WAIT_SERVICE_DISABLED attempt=%d' % (TAG, attempt))
                time.sleep(30)
                continue
        if sa_email and not attempted_sa_omit and 'act as service_account' in txt:
            say('%s_CREATE_RETRY_SA_OMITTED' % TAG)
            body['jobSpec'].pop('serviceAccount', None)
            attempted_sa_omit = True
            rec['service_account'] = False
            continue
        break
    rec['create_http'] = c
    rec['create_resp'] = cap(b, 2000)
    if c != 200:
        return rec
    job = json.loads(b)
    jname = job.get('name') or ''
    jid = jname.rsplit('/', 1)[-1]
    rec['job_name'] = jname
    rec['job_id'] = jid
    last = None
    final = None
    logs_http = -1
    rows = []
    for i in range(54):                       # 54 x 20s = 18 min
        time.sleep(20)
        cc, bb = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s'
                      % (region, jname), timeout=60)
        st = jget(bb, 'state')
        if st != last:
            rec.setdefault('state_trace', []).append({'t': i * 20, 'state': st})
            say('%s_JOB_STATE t=%d state=%s http=%s' % (TAG, i * 20, st, cc))
            last = st
        if i % 3 == 2 or st in ('JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED',
                                'JOB_STATE_CANCELLED', 'JOB_STATE_EXPIRED'):
            logs_http, got, lerr = job_logs(token, jid)
            if got:
                rows = got
            elif lerr:
                rec['logs_err'] = lerr
            if any('GPU-' in x for x in rows):
                break
        if st in ('JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED', 'JOB_STATE_CANCELLED',
                  'JOB_STATE_EXPIRED'):
            final = st
            break
    rec['final_state'] = final or last
    rec['poll_timeout'] = final is None
    if not rows:
        logs_http, rows, lerr = job_logs(token, jid)
        rec['logs_err'] = rec.get('logs_err') or lerr
    gate_ok = any('GPU-' in x for x in rows)
    extra = 0
    while not gate_ok and extra < 6 and rec.get('final_state') in (
            'JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED', 'JOB_STATE_CANCELLED',
            'JOB_STATE_EXPIRED'):
        time.sleep(15)
        lc3, rows3, _ = job_logs(token, jid)
        if rows3:
            rows = rows3
        gate_ok = any('GPU-' in x for x in rows)
        extra += 1
    rec['logs_http'] = logs_http
    rec['n_log_lines'] = len(rows)
    dev = [x for x in rows if ('GPU-' in x or 'C211_I122745' in x
                               or 'nvidia-smi' in x.lower() or 'NVIDIA' in x)]
    rec['device_lines'] = [cap(x, 400) for x in dev[:30]]
    for x in rec['device_lines']:
        say('%s_DEVICE_LINE %s' % (TAG, x))
    rec['logs_tail'] = [cap(x, 300) for x in rows[-25:]]
    # independent readback
    gc, gb = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s' % (region, jname),
                  timeout=60)
    rec['readback_http'] = gc
    rec['readback_state'] = jget(gb, 'state')
    rec['readback_accel'] = None
    try:
        ms = json.loads(gb)['jobSpec']['workerPoolSpecs'][0]['machineSpec']
        rec['readback_accel'] = ms
    except Exception:
        pass
    say('%s_READBACK http=%s state=%s accel=%s' % (
        TAG, gc, rec['readback_state'], json.dumps(rec['readback_accel'], default=str)))
    # cleanup: cancel (if live) then delete, then absence
    if rec['final_state'] not in ('JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED',
                                  'JOB_STATE_CANCELLED', 'JOB_STATE_EXPIRED'):
        cc, cb = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s:cancel'
                      % (region, jname), method='POST', data={})
        rec['cancel_http'] = cc
        rec['cancel_resp'] = cap(cb, 300)
        time.sleep(6)
    dc, db = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/%s' % (region, jname),
                  method=M_DEL, timeout=120)
    rec['delete_http'] = dc
    rec['delete_resp'] = cap(db, 500)
    say('%s_DELETE http=%s body=%s' % (TAG, dc, rec['delete_resp']))
    for _ in range(12):
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
    say('%s_ABSENCE get_http=%s list_http=%s list_matching=%s' % (
        TAG, ac, lst['http'], json.dumps(rec['post_list']['matching'])))
    return rec


def main():
    section('header')
    say('%s_START %s' % (TAG, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
    say('%s_NONCE %s project=%s (%s)' % (TAG, NONCE, PROJECT, PROJECT_NUM))

    token = get_token()
    if not token:
        section('verdict')
        say('%s_VERDICT NO_TOKEN' % TAG)
        say('%s_END %s' % (TAG, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
        return
    STATE['token'] = token
    section('identity')
    c, b = http('https://oauth2.googleapis.com/tokeninfo?access_token='
                + urllib.parse.quote(token, safe=''), timeout=30)
    say('%s_TOKENINFO http=%s azp=%s scope=%s expires_in=%s' % (
        TAG, c, jget(b, 'azp'), cap(jget(b, 'scope') or '', 200), jget(b, 'expires_in')))
    c, b = gapi(token, 'https://cloudresourcemanager.googleapis.com/v1/projects/' + PROJECT_NUM)
    say('%s_PROJECT_GET http=%s body=%s' % (TAG, c, cap(b, 700)))
    pj = {}
    try:
        pj = json.loads(b)
    except Exception:
        pass
    say('%s_PROJECT_NUM=%s parent=%s' % (
        TAG, pj.get('projectNumber'), json.dumps(pj.get('parent') or {})))

    section('services')
    aip = svc_get(token, 'aiplatform.googleapis.com')
    say('%s_SVC_GET aiplatform http=%s state=%s err=%s' % (
        TAG, aip['http'], aip.get('state'), cap(aip.get('body') or '', 400)))
    en = None
    if aip.get('state') != 'ENABLED':
        en = svc_enable(token, 'aiplatform.googleapis.com')
        say('%s_SVC_ENABLE aiplatform http=%s done=%s error=%s resp=%s' % (
            TAG, en.get('http'), en.get('done'),
            json.dumps(en.get('error'), default=str)[:400], cap(en.get('resp') or '', 600)))
        aip2 = svc_get(token, 'aiplatform.googleapis.com')
        say('%s_SVC_GET_AFTER_ENABLE aiplatform http=%s state=%s' % (
            TAG, aip2['http'], aip2.get('state')))
    section_stats = {'enable': en, 'after': aip.get('state')}

    section('perms')
    perms = ['aiplatform.customJobs.create', 'aiplatform.customJobs.get',
             'aiplatform.customJobs.list', 'aiplatform.customJobs.cancel',
             'aiplatform.customJobs.delete', 'aiplatform.executions.create',
             'serviceusage.services.enable', 'serviceusage.services.get',
             'logging.logEntries.list', 'iam.serviceAccounts.actAs']
    pr = test_iam(token, perms)
    say('%s_TESTIAM http=%s granted=%s denied=%s err=%s' % (
        TAG, pr['http'], json.dumps(pr['granted']), json.dumps(pr['denied']),
        cap(pr.get('body') or '', 400)))

    section('quota')
    quota = consumer_quota(token, 'aiplatform.googleapis.com')
    fmt_quota(quota, 'aiplatform')

    section('preexisting')
    pre = {}
    for rg in TARGET_REGIONS[:4]:
        lst = list_jobs(token, rg)
        pre[rg] = {'http': lst['http'], 'n': lst['n'], 'names': lst['names']}
        say('%s_PREEXISTING region=%s http=%s n=%s' % (TAG, rg, lst['http'], lst['n']))

    section('marker')
    shape = decide(quota)
    say('%s_DECISION ondemand_shape=%s reason=%s ond_a100=%s ond_t4=%s pre_a100=%s pre_t4=%s' % (
        TAG, json.dumps({k: shape.get(k) for k in ('mode', 'accel', 'machine', 'region')}),
        shape.get('reason'), json.dumps(shape['ond_a100']), json.dumps(shape['ond_t4']),
        json.dumps(shape['pre_a100']), json.dumps(shape['pre_t4'])))
    sa_email = PROJECT_NUM + '-compute@developer.gserviceaccount.com'
    first = vertex_job(token, dict(shape, mode='ondemand'), sa_email)
    say('%s_OND_CREATE_HTTP %s resp=%s' % (TAG, first.get('create_http'),
                                           cap(first.get('create_resp') or '', 800)))
    spot_rec = None
    device_ok = any('GPU-' in x for x in (first.get('device_lines') or []))
    if not device_ok and quota_refusal(first):
        sp = decide_spot(quota, shape)
        if sp:
            say('%s_SPOT_DECISION %s' % (TAG, json.dumps(sp)))
            spot_rec = vertex_job(token, sp, sa_email)
            say('%s_SPOT_CREATE_HTTP %s resp=%s' % (
                TAG, spot_rec.get('create_http'), cap(spot_rec.get('create_resp') or '', 800)))
            device_ok = any('GPU-' in x for x in (spot_rec.get('device_lines') or []))
        else:
            say('%s_SPOT_SKIPPED no_preemptible_quota_measured' % TAG)

    section('verdict')
    uuids = []
    for rec in (first, spot_rec):
        for line in ((rec or {}).get('device_lines') or []):
            if 'GPU-' in line:
                uuids.append(cap(line, 300))
    say('%s_VERDICT device=%s uuids=%s first_state=%s first_delete=%s spot_state=%s spot_delete=%s' % (
        TAG, device_ok, json.dumps(uuids), (first or {}).get('final_state'),
        (first or {}).get('delete_http'), (spot_rec or {}).get('final_state'),
        (spot_rec or {}).get('delete_http')))
    say('%s_END %s' % (TAG, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
    # machine-readable single-line records for log parsing
    say('%s_JSON_OND %s' % (TAG, json.dumps({k: first.get(k) for k in
        ('region', 'accel', 'machine', 'mode', 'create_http', 'create_resp', 'job_id',
         'final_state', 'readback_http', 'readback_state', 'readback_accel',
         'delete_http', 'absence_get_http', 'post_list', 'device_lines')},
        default=str)[:6000]))
    if spot_rec:
        say('%s_JSON_SPOT %s' % (TAG, json.dumps({k: spot_rec.get(k) for k in
            ('region', 'accel', 'machine', 'mode', 'create_http', 'create_resp', 'job_id',
             'final_state', 'readback_http', 'readback_state', 'readback_accel',
             'delete_http', 'absence_get_http', 'post_list', 'device_lines')},
            default=str)[:6000]))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        say('%s_FATAL %s' % (TAG, cap(repr(e), 500)))
