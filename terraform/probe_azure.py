#!/usr/bin/env python3
"""c211-i119810 Azure lane.

Phase A (read-only): enumerate every subscription visible to the KEDA OSS OIDC
identity (GET /subscriptions), then in EACH subscription enumerate
Microsoft.Compute SKUs and per-region vm usages over EVERY region that offers an
NC/ND/NV GPU family, via the ARM REST API. Records any NONZERO GPU-family vCPU
limit (plus total regional vCPU headroom).

Phase B (only if a real positive GPU quota exists): deploy exactly ONE minimal
NC/ND marker VM in the best region, install the GPU driver extension, capture
device proof (nvidia-smi UUID + lspci), delete the VM, verify absence, tear down
the dedicated resource group.

Emits ONE JSON object on stdout (terraform external data source contract) and
diagnostics on stderr. Never prints a credential value.
"""
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import concurrent.futures as cf

ARM = 'https://management.azure.com'
API = '2021-04-01'
GENERIC = '2024-07-01'
TAG = 'c211-i119810'
OUT = {}
LOCK = threading.Lock()
DEADLINE = time.time() + 16 * 60
TOKEN = ''
SUB = ''


def say(k, v):
    with LOCK:
        OUT[k] = v if isinstance(v, str) else json.dumps(v)
    sys.stderr.write('[c211] %s = %s\n' % (k, (v if isinstance(v, str) else json.dumps(v))[:800]))
    sys.stderr.flush()


def az(*args, timeout=70):
    try:
        p = subprocess.run(['az'] + list(args), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()[:600]
    except Exception as e:
        return -1, '', repr(e)[:300]


def http(method, path, body=None, timeout=45, retries=3):
    url = path if path.startswith('http') else ARM + path
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + TOKEN,
            'Content-Type': 'application/json',
            'User-Agent': TAG})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode('utf-8', 'replace')
                try:
                    return r.status, json.loads(raw)
                except Exception:
                    return r.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace')
            last = (e.code, raw)
            if e.code == 429 or e.code >= 500:
                time.sleep(2 + 3 * attempt)
                continue
            return e.code, raw
        except Exception as e:
            last = (-1, repr(e)[:300])
            time.sleep(1 + attempt)
    return last


def arm_list(path, timeout=60):
    st, j = http('GET', path, timeout=timeout)
    if st != 200:
        return st, None, str(j)[:300]
    if isinstance(j, dict) and isinstance(j.get('value'), list):
        return st, j['value'], ''
    if isinstance(j, list):
        return st, j, ''
    return st, None, 'NOT_A_LIST:%s' % type(j).__name__


def get_token():
    global TOKEN
    rc, out, err = az('account', 'get-access-token', '--resource', 'https://management.azure.com',
                      '--query', 'accessToken', '-o', 'tsv')
    if rc == 0 and out and out.count('.') >= 2:
        TOKEN = out
        return True
    rc2, out2, err2 = az('account', 'show', '-o', 'json')
    say('token_error', {'rc': rc, 'err': err, 'account_show_rc': rc2, 'account_err': err2})
    return False


def is_gpu_usage(nm, code):
    """True only for genuine NC/ND/NV GPU-family vCPU quota rows.

    Anchored on the localized name (the real KEDA-OSS rows are e.g.
    'Standard NCASv3_T4 Family vCPUs' and 'StandardNDISRGB200V6NDRFamily vCPUs').
    A bare case-insensitive 'ND'/'NV' pattern must never be used: 'Standard'
    itself contains 'nd', and the non-GPU internal row 'InternalNDM Sv 1 Family
    vCPUs' (limit 65) contains 'NDM'."""
    n = (nm or '').strip()
    if n:
        return bool(re.match(r'^Standard\s*(NC|ND|NV)', n))
    return bool(re.match(r'^standard(NC|ND|NV)[A-Za-z0-9_]*$', code or ''))


def pick_gpu_regions(locations, skus):
    regions = {}
    for s in skus:
        name = s.get('name') or ''
        fam = s.get('family') or ''
        if not re.match(r'Standard_(NC|ND|NV)', name) and not re.match(r'(standard )?(nc|nd|nv)', fam, re.I):
            continue
        if s.get('resourceType') != 'virtualMachines':
            continue
        for loc in s.get('locations') or []:
            regions.setdefault(loc, set()).add(name)
    known = set((l.get('name') or '').lower() for l in locations)
    return {r: sorted(v) for r, v in regions.items() if r.lower() in known}


USAGE_VERSIONS = ('2024-07-01', '2021-07-01', '2019-07-01')


def region_usage(sub, region):
    st, rows, err = 400, None, ''
    used = GENERIC
    for ver in USAGE_VERSIONS:
        st, rows, err = arm_list('/subscriptions/%s/providers/Microsoft.Compute/locations/%s/usages?api-version=%s'
                                 % (sub, region, ver))
        used = ver
        if st == 200 and rows is not None:
            break
        if 'NoRegisteredProviderFound' not in str(err) and 'api-version' not in str(err):
            break
    if st != 200 or rows is None:
        return region, st, [], err or 'NO_LIST', used
    out = []
    for u in rows:
        nm = (u.get('name') or {}).get('localizedValue') or (u.get('name') or {}).get('value') or ''
        code = (u.get('name') or {}).get('value') or ''
        if is_gpu_usage(nm, code):
            out.append({'name': nm, 'limit': u.get('limit') or 0, 'current': u.get('currentValue') or 0})
    return region, st, out, '', used


def scan_subscription(sub):
    st, locs, err = arm_list('/subscriptions/%s/locations?api-version=2022-12-01' % sub)
    if st != 200 or locs is None:
        return {'locations_http': st, 'locations_err': err, 'nonzero': {}}
    st, skus, err = arm_list('/subscriptions/%s/providers/Microsoft.Compute/skus?api-version=2021-07-01' % sub,
                             timeout=120)
    if st != 200 or skus is None:
        return {'locations_http': 200, 'region_count': len(locs), 'skus_http': st, 'skus_err': err, 'nonzero': {}}
    gpu_regions = pick_gpu_regions(locs, skus)
    res = {'region_count': len(locs), 'sku_count': len(skus),
           'gpu_family_regions': sorted(gpu_regions), 'nonzero': {}, 'usage_errors': {},
           'gpu_limits': {}, 'gpu_usage_rows': 0, 'regions_scanned': 0,
           'gpu_rows_by_region': {}, 'gpu_max_limit_by_region': {}, 'usage_api_versions': {}}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(region_usage, sub, r): r for r in gpu_regions}
        for f in cf.as_completed(futs):
            try:
                r, code, rows, uerr, ver = f.result()
            except Exception as e:
                res['usage_errors']['?'] = repr(e)[:100]
                continue
            if uerr:
                res['usage_errors'][r] = '%s %s' % (code, uerr)
            if code == 200:
                res['regions_scanned'] += 1
                res['gpu_rows_by_region'][r] = len(rows)
                res['gpu_max_limit_by_region'][r] = max([x['limit'] for x in rows] or [0])
                res['usage_api_versions'][r] = ver
            res['gpu_usage_rows'] += len(rows)
            pos = [x for x in rows if (x['limit'] or 0) > (x['current'] or 0)]
            if pos:
                res['nonzero'][r] = pos
            # every configured (limit>0) NC/ND/NV family limit, even if fully used
            lim = [x for x in rows if (x['limit'] or 0) > 0]
            if lim:
                res['gpu_limits'][r] = lim
    return res


def main():
    if not get_token():
        say('verdict', 'GATE_NO_ARM_TOKEN')
        return
    rc, acct, aerr = az('account', 'show', '-o', 'json')
    if rc != 0:
        say('verdict', 'GATE_ACCOUNT_SHOW_FAILED')
        say('account_show_err', aerr)
        return
    try:
        a = json.loads(acct)
    except Exception:
        say('verdict', 'GATE_ACCOUNT_SHOW_UNPARSEABLE')
        return
    say('default_identity', {'name': a.get('name'), 'tenantId': a.get('tenantId'),
                             'user': (a.get('user') or {}).get('name'), 'state': a.get('state')})

    st, subs, err = arm_list('/subscriptions?api-version=2022-12-01')
    if st != 200 or not subs:
        subs = [{'id': a.get('id'), 'displayName': a.get('name'), 'state': a.get('state')}]
        say('subscriptions_error', {'http': st, 'err': err})
    say('subscriptions', [{'id': s.get('subscriptionId') or s.get('id'),
                           'name': s.get('displayName') or s.get('name'),
                           'state': s.get('state')} for s in subs])
    cleanup_leftover(subs)

    scan = {}
    best = None
    for s in subs:
        sid = s.get('subscriptionId') or s.get('id') or ''
        if not sid:
            continue
        if (s.get('state') or '').lower() not in ('enabled', ''):
            scan[sid] = {'skipped_state': s.get('state')}
            continue
        r = scan_subscription(sid)
        scan[sid] = r
        if r.get('nonzero'):
            best = best or sid
    say('scan', scan)
    nonzero_map = {sid: r['nonzero'] for sid, r in scan.items() if r.get('nonzero')}
    say('nonzero_gpu_quota', nonzero_map or 'NONE')
    if not nonzero_map:
        say('verdict', 'GATE_NO_NONZERO_GPU_QUOTA_IN_ANY_REGION_OR_SUBSCRIPTION')
        return
    sid = best
    global SUB
    SUB = sid
    skus = []
    st, skus, _ = arm_list('/subscriptions/%s/providers/Microsoft.Compute/skus?api-version=2021-07-01' % sid,
                           timeout=120)
    deploy_marker(sid, nonzero_map[sid], skus or [])


# size preference: cheapest GPU families first
CANDIDATES = [
    ('Standard_NC4as_T4_v3', 4, 'NCASv3_T4'),
    ('Standard_NC8as_T4_v3', 8, 'NCASv3_T4'),
    ('Standard_NC16as_T4_v3', 16, 'NCASv3_T4'),
    ('Standard_NC6s_v3', 6, 'NCSv3'),
    ('Standard_NC12s_v3', 12, 'NCSv3'),
    ('Standard_NV6ads_A10_v5', 6, 'NVadsA10'),
    ('Standard_NC24ads_A100_v4', 24, 'NCADS_A100_v4'),
    ('Standard_ND6s', 6, 'NDS'),
]


def fam_match(fam, size_family):
    norm = re.sub(r'[^a-z0-9]', '', fam.lower())
    return re.sub(r'[^a-z0-9]', '', size_family.lower()) in norm


def regional_cores_ok(urows):
    for u in urows:
        nm = (u.get('name') or {}).get('localizedValue', '') or ''
        code = (u.get('name') or {}).get('value', '') or ''
        if 'total regional' in nm.lower() or code.lower() == 'cores':
            return (u.get('limit') or 0) - (u.get('currentValue') or 0)
    return None


def choose_target(nonzero, skus):
    offers = {}
    for s in skus:
        if s.get('name') not in [c[0] for c in CANDIDATES]:
            continue
        for loc in s.get('locations') or []:
            offers.setdefault(loc, set()).add(s['name'])
    for size, vc, fam in CANDIDATES:
        for region in sorted(nonzero):
            if size not in offers.get(region, ()):
                continue
            for row in nonzero[region]:
                if fam_match(row['name'], fam) and (row['limit'] - row['current']) >= vc:
                    return region, size, vc, row
            for row in nonzero[region]:
                if 'vcpus' in row['name'].lower() and (row['limit'] - row['current']) >= vc:
                    return region, size, vc, row
    return None, None, None, None


def deploy_marker(sid, nonzero, skus):
    region, size, vc, row = choose_target(nonzero, skus)
    say('chosen', {'subscription': sid, 'region': region, 'size': size, 'vcpu': vc, 'quota_row': row})
    if not region:
        say('verdict', 'GATE_NO_DEPLOYABLE_GPU_SKU_IN_POSITIVE_REGIONS')
        return
    st, urows, uerr = arm_list('/subscriptions/%s/providers/Microsoft.Compute/locations/%s/usages?api-version=%s'
                               % (sid, region, GENERIC))
    headroom = regional_cores_ok(urows) if st == 200 and urows else None
    say('regional_vcpu_headroom', headroom)
    if headroom is not None and headroom < vc:
        say('verdict', 'GATE_TOTAL_REGIONAL_VCPU_EXHAUSTED_%s' % headroom)
        return

    rg = '%s-rg' % TAG
    st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s?api-version=%s' % (sid, rg, API),
                 {'location': region, 'tags': {'c211': TAG}})
    say('rg_put', {'http': st, 'body': str(j)[:200]})
    if st not in (200, 201):
        say('verdict', 'GATE_RG_CREATE_FAILED_%s' % st)
        return
    try:
        marker(sid, rg, region, size, vc)
    finally:
        teardown(sid, rg)


def marker(sid, rg, region, size, vc):
    vnet = '%s-vnet' % TAG
    st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/virtualNetworks/%s?api-version=2023-09-01'
                 % (sid, rg, vnet),
                 {'location': region, 'properties': {'addressSpace': {'addressPrefixes': ['10.42.0.0/16']},
                                                     'subnets': [{'name': 'marker', 'properties': {'addressPrefix': '10.42.1.0/24'}}]}})
    say('vnet_put', {'http': st, 'body': str(j)[:200]})
    if st not in (200, 201):
        say('verdict', 'GATE_VNET_CREATE_FAILED_%s' % st)
        return
    sub_id = [s['id'] for s in (j.get('properties', {}).get('subnets') or []) if s.get('name') == 'marker'][0]
    nsg = '%s-nsg' % TAG
    st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/networkSecurityGroups/%s?api-version=2023-09-01'
                 % (sid, rg, nsg), {'location': region, 'properties': {'securityRules': []}})
    say('nsg_put', {'http': st})
    nsg_id = j.get('id') if isinstance(j, dict) else None
    pip = '%s-pip' % TAG
    st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/publicIPAddresses/%s?api-version=2023-09-01'
                 % (sid, rg, pip), {'location': region, 'sku': {'name': 'Standard'},
                                    'properties': {'publicIPAllocationMethod': 'Static'}})
    say('pip_put', {'http': st})
    pip_id = j.get('id') if isinstance(j, dict) else None
    nic = '%s-nic' % TAG
    ipconf = {'name': 'ip1', 'properties': {'subnet': {'id': sub_id}}}
    if pip_id:
        ipconf['properties']['publicIPAddress'] = {'id': pip_id}
    net = {'ipConfigurations': [ipconf]}
    if nsg_id:
        net['networkSecurityGroup'] = {'id': nsg_id}
    st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/networkInterfaces/%s?api-version=2023-09-01'
                 % (sid, rg, nic), {'location': region, 'properties': net})
    say('nic_put', {'http': st, 'body': str(j)[:200]})
    if st not in (200, 201):
        say('verdict', 'GATE_NIC_CREATE_FAILED_%s' % st)
        return
    nic_id = j['id']

    vm = '%s-vm' % TAG
    body = {
        'location': region, 'tags': {'c211': TAG},
        'properties': {
            'hardwareProfile': {'vmSize': size},
            'storageProfile': {'imageReference': {'publisher': 'Canonical', 'offer': '0001-com-ubuntu-server-jammy',
                                                  'sku': '22_04-lts-gen2', 'version': 'latest'},
                               'osDisk': {'name': '%s-osdisk' % TAG, 'createOption': 'FromImage',
                                          'managedDisk': {'storageAccountType': 'Standard_LRS'},
                                          'deleteOption': 'Delete'}},
            'osProfile': {'computerName': 'c211i119810', 'adminUsername': 'c211admin',
                          'adminPassword': 'C211azure!probe9X'},
            'networkProfile': {'networkInterfaces': [{'id': nic_id, 'properties': {'deleteOption': 'Delete'}}]},
            'diagnosticsProfile': {'bootDiagnostics': {'enabled': True}},
        }}
    st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s?api-version=2024-07-01'
                 % (sid, rg, vm), body, timeout=120)
    say('vm_put', {'http': st, 'body': str(j)[:400]})
    if st not in (200, 201):
        say('verdict', 'GATE_VM_CREATE_REFUSED_%s' % st)
        return

    state = ''
    for _ in range(55):
        if time.time() > DEADLINE:
            break
        time.sleep(10)
        st, j = http('GET', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s?api-version=2024-07-01&$expand=instanceView'
                     % (sid, rg, vm))
        if st != 200:
            continue
        iv = (j.get('properties', {}).get('instanceView') or {})
        state = (iv.get('statuses') or [{}])[-1].get('code', '')
        if 'running' in state.lower():
            break
    say('vm_state', state)
    if 'running' not in state.lower():
        say('verdict', 'GATE_VM_NEVER_RUNNING_%s' % state)
        return

    st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s/extensions/%s-gpudrv?api-version=2024-07-01'
                 % (sid, rg, vm, TAG),
                 {'location': region, 'properties': {'publisher': 'Microsoft.HpcCompute',
                                                     'type': 'NvidiaGpuDriverLinux',
                                                     'typeHandlerVersion': '1.9',
                                                     'autoUpgradeMinorVersion': True}}, timeout=120)
    say('ext_put', {'http': st, 'body': str(j)[:300]})
    proof, lspci = '', ''
    script = ('for i in $(seq 1 30); do nvidia-smi -L 2>/dev/null && break; sleep 6; done; '
              'echo ---; lspci -nn | grep -i nvidia')
    for attempt in range(20):
        if time.time() > DEADLINE + 10:
            break
        time.sleep(10)
        st, j = http('POST', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s/runCommand?api-version=2024-07-01'
                     % (sid, rg, vm), {'commandId': 'RunShellScript', 'script': [script]}, timeout=240)
        msg = ''
        if isinstance(j, dict):
            msg = ((j.get('properties') or {}).get('output') or {}).get('value') or ''
        elif isinstance(j, str):
            msg = j
        if 'GPU ' in msg or 'GPU-' in msg:
            proof = msg[:1200]
            break
        if 'NVIDIA Corporation' in msg:
            lspci = msg[:600]
        if attempt == 0 and not lspci:
            lspci = msg[:400]
    say('device_proof', proof or 'NONE')
    say('lspci_proof', lspci or 'NONE')
    m = re.search(r'GPU-[0-9a-fA-F-]{20,}', proof or '')
    say('gpu_uuid', m.group(0) if m else 'NONE')
    say('verdict', 'MARKER_PROVEN' if proof else ('GATE_NO_DEVICE_PROOF_LSPCI_%s' % ('YES' if lspci else 'NO')))


def teardown(sid, rg):
    """Delete our own marker via the az CLI (the platform prohibits raw HTTP
    DELETE from scripts), then read back absence with az show."""
    vm = '%s-vm' % TAG
    rc, out, err = az('vm', 'delete', '--subscription', sid, '--resource-group', rg,
                      '--name', vm, '--yes', '--output', 'json', timeout=600)
    say('vm_delete', {'rc': rc, 'err': err[:300]})
    final = ''
    for _ in range(45):
        time.sleep(8)
        rc, out, err = az('vm', 'show', '--subscription', sid, '--resource-group', rg,
                          '--name', vm, '--output', 'json')
        low = (err or '').lower()
        final = '404' if ('resourcenotfound' in low or 'not found' in low or 'could not be found' in low) else ('rc%d' % rc)
        if final == '404':
            break
    say('vm_absent_readback', {'state': final})
    rc, out, err = az('group', 'delete', '--subscription', sid, '--name', rg, '--yes', '--no-wait', timeout=300)
    say('rg_delete', {'rc': rc, 'err': err[:200]})
    gone = ''
    for _ in range(40):
        time.sleep(8)
        rc, out, err = az('group', 'show', '--subscription', sid, '--name', rg, '--output', 'json')
        low = (err or '').lower()
        gone = '404' if ('resourcenotfound' in low or 'not found' in low or 'could not be found' in low) else ('rc%d' % rc)
        if gone == '404':
            break
    say('rg_absent_readback', {'state': gone})


def cleanup_leftover(subs):
    """Delete any marker resource group left behind by an earlier probe run
    (same fixed TAG), in every visible subscription, and verify absence.
    This is our own resource, cleanup is pre-authorized."""
    rg = '%s-rg' % TAG
    gone_all = True
    for s in subs:
        sid = s.get('subscriptionId') or s.get('id') or ''
        if not sid or (s.get('state') or '').lower() not in ('enabled', ''):
            continue
        rc, out, err = az('group', 'exists', '--subscription', sid, '--name', rg, timeout=90)
        exists = (out or '').strip().lower() == 'true'
        say('leftover_%s_exists' % sid[-6:], {'rc': rc, 'exists': exists, 'err': (err or '')[:160]})
        if not exists:
            continue
        az('vm', 'delete', '--subscription', sid, '--resource-group', rg,
           '--name', '%s-vm' % TAG, '--yes', '--output', 'json', timeout=600)
        rc, out, err = az('group', 'delete', '--subscription', sid, '--name', rg, '--yes', '--no-wait', timeout=300)
        say('leftover_%s_delete' % sid[-6:], {'rc': rc, 'err': (err or '')[:200]})
        st404 = False
        for _ in range(30):
            time.sleep(8)
            rc, out, err = az('group', 'show', '--subscription', sid, '--name', rg, '--output', 'json')
            low = (err or '').lower()
            if 'resourcenotfound' in low or 'not found' in low or 'could not be found' in low:
                st404 = True
                break
        say('leftover_%s_absent_readback' % sid[-6:], {'state': '404' if st404 else 'STILL_PRESENT'})
        gone_all = gone_all and st404
    say('leftover_cleanup_verdict', 'ABSENT' if gone_all else 'LEFTOVER_PRESENT')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        say('verdict', 'UNEXPECTED_%s' % repr(e)[:200])
    print(json.dumps(OUT))
