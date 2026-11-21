#!/usr/bin/env python3
"""c211-i119810 Azure lane.

Phase A (read-only): enumerate Microsoft.Compute resource SKUs and per-region
vm usages over EVERY region that offers an NC/ND/NV GPU family, via the ARM
REST API with the OIDC token that azure/login@v3 already minted in this job.
Records any NONZERO GPU-family vCPU limit (plus total regional vCPU headroom).

Phase B (only if a real positive GPU quota exists): deploy exactly ONE minimal
NC/ND marker VM in the best region, install the GPU driver extension, capture
device proof (nvidia-smi UUID + lspci), delete the VM, verify absence, and tear
down anything else it created.

Emits ONE JSON object on stdout (terraform external data source contract).
Never prints a credential value.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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


def say(k, v):
    with LOCK:
        OUT[k] = v if isinstance(v, str) else json.dumps(v)
    sys.stderr.write('[c211] %s = %s\n' % (k, (v if isinstance(v, str) else json.dumps(v))[:600]))
    sys.stderr.flush()


def az(*args, timeout=70):
    try:
        p = subprocess.run(['az'] + list(args), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()[:600]
    except Exception as e:
        return -1, '', repr(e)[:300]


def http(method, path, body=None, timeout=45, retries=3):
    """path is either a full URL or a path under management.azure.com."""
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


GPU_RE = re.compile(r'(NCA|NCC|NCS|NCAD|ND|NV|NC\b|GPU)', re.I)


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


def region_usage(region):
    st, j = http('GET', '/subscriptions/%s/providers/Microsoft.Compute/locations/%s/usages?api-version=%s'
                 % (SUB, region, GENERIC))
    if st != 200 or not isinstance(j, list):
        return region, st, [], str(j)[:200]
    rows = []
    for u in j:
        nm = (u.get('name') or {}).get('localizedValue') or (u.get('name') or {}).get('value') or ''
        lim = u.get('limit') or 0
        cur = u.get('currentValue') or 0
        if GPU_RE.search(nm) or GPU_RE.search((u.get('name') or {}).get('value') or ''):
            rows.append({'name': nm, 'limit': lim, 'current': cur})
    return region, st, rows, ''


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
    global SUB
    SUB = a.get('id') or ''
    say('identity', {'name': a.get('name'), 'id': SUB, 'tenantId': a.get('tenantId'),
                     'user': (a.get('user') or {}).get('name'), 'state': a.get('state')})

    st, locs = http('GET', '/subscriptions/%s/locations?api-version=2022-12-01' % SUB)
    if st != 200 or not isinstance(locs, list):
        say('verdict', 'GATE_LOCATIONS_FAILED_%s' % st)
        say('locations_body', str(locs)[:400])
        return
    say('region_count', len(locs))

    st, skus = http('GET', '/subscriptions/%s/providers/Microsoft.Compute/skus?api-version=2021-07-01' % SUB,
                    timeout=90)
    if st != 200 or not isinstance(skus, list):
        say('verdict', 'GATE_SKUS_FAILED_%s' % st)
        say('skus_body', str(skus)[:400])
        return
    say('sku_count', len(skus))

    gpu_regions = pick_gpu_regions(locs, skus)
    say('gpu_family_regions', sorted(gpu_regions))

    results = {}
    nonzero = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(region_usage, r): r for r in gpu_regions}
        for f in cf.as_completed(futs):
            try:
                r, code, rows, err = f.result()
            except Exception as e:
                continue
            results[r] = {'http': code, 'n': len(rows), 'err': err}
            pos = [x for x in rows if (x['limit'] or 0) > (x['current'] or 0)]
            if pos:
                nonzero[r] = pos
    say('usage_probed_regions', sorted(results))
    say('usage_errors', {r: v['err'] for r, v in results.items() if v['err']})
    say('nonzero_gpu_quota_regions', nonzero or 'NONE')
    if not nonzero:
        say('verdict', 'GATE_NO_NONZERO_GPU_QUOTA_IN_ANY_REGION')
        return

    # ---- Phase B: one marker ----
    deploy_marker(nonzero, skus, locs)


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


def regional_cores_ok(region_usage_rows):
    for u in region_usage_rows:
        nm = (u.get('name') or '').lower()
        val = ((u.get('name') or {}).get('value') or '').lower()
        if 'total regional' in nm or val == 'cores':
            return (u.get('limit') or 0) - (u.get('current') or 0)
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
            # also accept a generous generic NC row
            for row in nonzero[region]:
                if 'vcpus' in row['name'].lower() and (row['limit'] - row['current']) >= vc:
                    return region, size, vc, row
    return None, None, None, None


def deploy_marker(nonzero, skus, locs):
    region, size, vc, row = choose_target(nonzero, skus)
    say('chosen', {'region': region, 'size': size, 'vcpu': vc, 'quota_row': row})
    if not region:
        say('verdict', 'GATE_NO_DEPLOYABLE_GPU_SKU_IN_POSITIVE_REGIONS')
        return
    st, urows = http('GET', '/subscriptions/%s/providers/Microsoft.Compute/locations/%s/usages?api-version=%s'
                     % (SUB, region, GENERIC))
    headroom = regional_cores_ok(urows) if st == 200 and isinstance(urows, list) else None
    say('regional_vcpu_headroom', headroom)
    if headroom is not None and headroom < vc:
        say('verdict', 'GATE_TOTAL_REGIONAL_VCPU_EXHAUSTED')
        return

    # --- resource group ---
    rg = '%s-rg' % TAG
    st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s?api-version=%s' % (SUB, rg, API),
                 {'location': region, 'tags': {'c211': TAG}})
    say('rg_put', {'http': st, 'body': str(j)[:200]})
    if st not in (200, 201):
        say('verdict', 'GATE_RG_CREATE_FAILED_%s' % st)
        return

    cleanup = {'rg': rg, 'vm': None, 'nic': None, 'disk': None, 'pip': None, 'nsg': None}
    try:
        # --- network: vnet + subnet + nsg + pip ---
        vnet = '%s-vnet' % TAG
        st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/virtualNetworks/%s?api-version=2023-09-01'
                     % (SUB, rg, vnet),
                     {'location': region, 'properties': {'addressSpace': {'addressPrefixes': ['10.42.0.0/16']},
                                                         'subnets': [{'name': 'marker', 'properties': {'addressPrefix': '10.42.1.0/24'}}]}})
        say('vnet_put', {'http': st, 'body': str(j)[:200]})
        if st not in (200, 201):
            say('verdict', 'GATE_VNET_CREATE_FAILED_%s' % st)
            return
        sub_id = [s['id'] for s in (j.get('properties', {}).get('subnets') or []) if s.get('name') == 'marker'][0]
        nsg = '%s-nsg' % TAG
        st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/networkSecurityGroups/%s?api-version=2023-09-01'
                     % (SUB, rg, nsg), {'location': region, 'properties': {'securityRules': []}})
        say('nsg_put', {'http': st})
        nsg_id = j.get('id') if isinstance(j, dict) else None
        pip = '%s-pip' % TAG
        st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/publicIPAddresses/%s?api-version=2023-09-01'
                     % (SUB, rg, pip), {'location': region, 'sku': {'name': 'Standard'},
                                        'properties': {'publicIPAllocationMethod': 'Static'}})
        say('pip_put', {'http': st})
        pip_id = j.get('id') if isinstance(j, dict) else None

        nic = '%s-nic' % TAG
        st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Network/networkInterfaces/%s?api-version=2023-09-01'
                     % (SUB, rg, nic),
                     {'location': region, 'properties': {
                         'ipConfigurations': [{'name': 'ip1', 'properties': {
                             'subnet': {'id': sub_id},
                             'publicIPAddress': {'id': pip_id}} if pip_id else {'subnet': {'id': sub_id}}}],
                         'networkSecurityGroup': {'id': nsg_id} if nsg_id else None}})
        say('nic_put', {'http': st, 'body': str(j)[:200]})
        if st not in (200, 201):
            say('verdict', 'GATE_NIC_CREATE_FAILED_%s' % st)
            return
        nic_id = j['id']
        cleanup['nic'] = nic_id

        vm = '%s-vm' % TAG
        cleanup['vm'] = vm
        vm_body = {
            'location': region,
            'tags': {'c211': TAG},
            'properties': {
                'hardwareProfile': {'vmSize': size},
                'storageProfile': {'imageReference': {
                    'publisher': 'Canonical', 'offer': '0001-com-ubuntu-server-jammy',
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
                     % (SUB, rg, vm), vm_body, timeout=90)
        say('vm_put', {'http': st, 'body': str(j)[:400]})
        if st not in (200, 201):
            say('verdict', 'GATE_VM_CREATE_REFUSED_%s' % st)
            return

        # poll for RUNNING
        state, iprop = '', {}
        for _ in range(60):
            if time.time() > DEADLINE:
                break
            time.sleep(10)
            st, j = http('GET', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s?api-version=2024-07-01&$expand=instanceView'
                         % (SUB, rg, vm))
            if st != 200:
                continue
            iprop = j.get('properties', {})
            state = (iprop.get('instanceView', {}).get('statuses') or [{}])[-1].get('code', '')
            if 'running' in state.lower():
                break
        say('vm_state', state)
        if 'running' not in state.lower():
            say('verdict', 'GATE_VM_NEVER_RUNNING_%s' % state)
            return

        # GPU driver extension (best effort) then device proof
        ext = '%s-gpudrv' % TAG
        st, j = http('PUT', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s/extensions/%s?api-version=2024-07-01'
                     % (SUB, rg, vm, ext),
                     {'location': region, 'properties': {'publisher': 'Microsoft.HpcCompute',
                                                         'type': 'NvidiaGpuDriverLinux',
                                                         'typeHandlerVersion': '1.9',
                                                         'autoUpgradeMinorVersion': True}}, timeout=90)
        say('ext_put', {'http': st, 'body': str(j)[:300]})
        proof = ''
        lspci = ''
        for attempt in range(24):
            if time.time() > DEADLINE + 5:
                break
            time.sleep(10)
            script = 'for i in $(seq 1 24); do nvidia-smi -L 2>/dev/null && break; sleep 5; done; echo ---; lspci -nn | grep -i nvidia'
            st, j = http('POST', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s/runCommand?api-version=2024-07-01'
                         % (SUB, rg, vm),
                         {'commandId': 'RunShellScript', 'script': [script]}, timeout=180)
            msg = ''
            if isinstance(j, dict):
                msg = ((j.get('properties') or {}).get('output') or {}).get('value') or ''
            if not msg and isinstance(j, str):
                msg = j
            if 'GPU ' in msg or 'GPU-' in msg or 'UUID' in msg:
                proof = msg[:1200]
                break
            if 'NVIDIA Corporation' in msg and 'lspci' in msg:
                lspci = msg[:600]
            if attempt == 2 and not lspci:
                lspci = msg[:400]
        say('device_proof', proof or 'NONE')
        say('lspci_proof', lspci or 'NONE')
        m = re.search(r'GPU-[0-9a-fA-F-]{20,}', proof or '')
        say('gpu_uuid', m.group(0) if m else 'NONE')
        say('verdict', 'MARKER_PROVEN' if proof else ('GATE_NO_DEVICE_PROOF_LSPCI_%s' % ('YES' if lspci else 'NO')))
    finally:
        teardown(cleanup)


def teardown(c):
    rg, vm = c['rg'], c['vm']
    if vm:
        st, j = http('DELETE', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s?api-version=2024-07-01'
                     % (SUB, rg, vm))
        say('vm_delete', {'http': st})
        final = ''
        for _ in range(40):
            time.sleep(8)
            st, j = http('GET', '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute/virtualMachines/%s?api-version=2024-07-01'
                         % (SUB, rg, vm))
            final = st
            if st == 404:
                break
        say('vm_absent_readback', {'http': final})
    # delete the dedicated RG (async) and verify VM/NIC gone
    st, j = http('DELETE', '/subscriptions/%s/resourceGroups/%s?api-version=%s' % (SUB, rg, API))
    say('rg_delete', {'http': st})
    gone = None
    for _ in range(30):
        time.sleep(8)
        st, j = http('GET', '/subscriptions/%s/resourceGroups/%s?api-version=%s' % (SUB, rg, API))
        gone = st
        if st == 404:
            break
    say('rg_absent_readback', {'http': gone})


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        say('verdict', 'UNEXPECTED_%s' % repr(e)[:200])
    print(json.dumps(OUT))
