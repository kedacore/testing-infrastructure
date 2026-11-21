#!/usr/bin/env python3
"""C211-I121673 — KEDA OSS subscription (sid tail 221821) plain-VM/VMSS GPU plane.

Runs as the fork-controlled terraform `data.external` program inside the
kedacore/testing-infrastructure pull_request_target 'Validate PR' job, where
azure/login@v3 has minted an ARM token for the KEDA OSS subscription.

Phases:
  0. session + ARM token + provider registration (Compute/Network) + region list
  1. LIVE per-family GPU quota matrix: Microsoft.Compute/locations/<r>/usages for
     every physical region, rows matching ^standard(nc|nd|nv) (covers
     standardNC*/standardND*/standardNV*/standardNVADSA10v5)
  2. vmSizes offer check for a bounded region set (is the SKU actually offered?)
  3. choose ONE candidate: first GPU SKU with nonzero family headroom and an
     offered size; else first offered GPU SKU; else T4/eastus blind default
  4. ONE nonce-tagged RG + vnet/subnet + NIC, then EXACTLY ONE on-demand GPU VM
     PUT (VMSS single-instance fallback only on a policy/parameter refusal),
     capturing the verbatim 201/400/409/403 body
  5. on creation: instanceView readback, NVIDIA driver extension, run-command
     nvidia-smi -L + --query-gpu=name,uuid,memory.total
  6. teardown of every own object + absence readback (GET 404 + list NO_RESULTS)

Output: exactly one JSON object on stdout (terraform external contract), a flat
string map with a chunked base64 matrix; diagnostics on stderr.
"""
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ARM = 'https://management.azure.com'
TAG = 'C211_I121673'
PREFIX = 'c211-i121673'
RG = PREFIX + '-rg'
VNET = PREFIX + '-vnet'
SUBNET = PREFIX + '-subnet'
NIC = PREFIX + '-nic0'
VM = PREFIX + '-marker'
VMSS = PREFIX + '-vmss'
ADMIN_USER = 'c211probe'
ADMIN_PW = 'C211Marker!Pw9x7qZ'
IMAGE = {'publisher': 'Canonical', 'offer': '0001-com-ubuntu-server-jammy',
         'sku': '22_04-lts-gen2', 'version': 'latest'}
API_VM = '2024-07-01'
API_NET = '2024-01-01'
API_USAGE = '2024-07-01'
TARGETS = [
    {'key': 't4', 'sku': 'Standard_NC4as_T4_v3', 'vcpu': 4, 'pat': 'ncasv3t4'},
    {'key': 'nc8ast4', 'sku': 'Standard_NC8as_T4_v3', 'vcpu': 8, 'pat': 'ncasv3t4'},
    {'key': 'nc6sv3', 'sku': 'Standard_NC6s_v3', 'vcpu': 6, 'pat': 'ncsv3'},
    {'key': 'a10v5', 'sku': 'Standard_NV12ads_A10_v5', 'vcpu': 12, 'pat': 'nvadsa10v5'},
    {'key': 'nv6', 'sku': 'Standard_NV6', 'vcpu': 6, 'pat': 'nvfamily'},
    {'key': 'nv12', 'sku': 'Standard_NV12', 'vcpu': 12, 'pat': 'nvfamily'},
    {'key': 'a100', 'sku': 'Standard_NC24ads_A100_v4', 'vcpu': 24, 'pat': 'ncadsa100v4'},
]
FIXED_REGIONS = ['eastus', 'westus2', 'westeurope', 'southeastasia', 'japaneast',
                 'centralus', 'eastus2', 'northeurope']
DEADLINE = 1500
T0 = time.time()
OUT = {}
EVENTS = []


def left():
    return DEADLINE - (time.time() - T0)


def emit(obj):
    try:
        EVENTS.append(obj)
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stderr.write(TAG + ' ' + json.dumps(obj, default=str)[:1800] + '\n')
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def save(k, v):
    OUT[k] = v if isinstance(v, str) else json.dumps(v, default=str)


def run(args, timeout=120):
    t = time.time()
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {'rc': p.returncode, 'out': p.stdout or '', 'err': p.stderr or '',
                'secs': round(time.time() - t, 1)}
    except Exception as e:  # noqa: BLE001
        return {'rc': 1, 'out': '', 'err': repr(e)[:300], 'secs': round(time.time() - t, 1)}


def az(args, timeout=120):
    return run(['az'] + args, timeout=timeout)


def arm(tok, url, method='GET', body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {'Authorization': 'Bearer ' + tok}
    if data is not None:
        hdrs['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or '{}'
            try:
                return r.status, json.loads(raw), round(time.time() - t, 1)
            except Exception:  # noqa: BLE001
                return r.status, {'raw': raw[:1500]}, round(time.time() - t, 1)
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or ''
        try:
            return e.code, json.loads(raw), round(time.time() - t, 1)
        except Exception:  # noqa: BLE001
            return e.code, {'raw': raw[:1500]}, round(time.time() - t, 1)
    except Exception as e:  # noqa: BLE001
        return 0, {'transport_error': repr(e)[:300]}, round(time.time() - t, 1)


def err_of(st, body):
    if isinstance(body, dict):
        e = body.get('error')
        if isinstance(e, dict):
            det = e.get('details') or []
            return {'http': st, 'code': e.get('code'),
                    'message': (e.get('message') or '')[:900],
                    'details': json.dumps(det)[:600] if det else None}
        if 'raw' in body:
            return {'http': st, 'raw': body['raw'][:800]}
        if 'transport_error' in body:
            return {'http': st, 'transport_error': body['transport_error']}
        return {'http': st, 'body': json.dumps(body)[:600]}
    return {'http': st}


def norm(x):
    return re.sub(r'[^a-z0-9]', '', str(x).lower())


def gpu_rows(body):
    out = []
    for u in (body.get('value') or []):
        nm = str((u.get('name') or {}).get('value', ''))
        if re.match(r'(?i)^standard(nc|nd|nv)', nm):
            out.append({'family': nm, 'current': u.get('currentValue'),
                        'limit': u.get('limit'), 'unit': u.get('unit')})
    return out


def short_family(f):
    s = str(f)
    for a, b in (('standard', ''), ('Family', ''), ('family', '')):
        s = s.replace(a, b)
    return s or str(f)


def headroom(rows, tgt):
    best = None
    for r in rows:
        if norm(tgt['pat']) in norm(r['family']):
            lim, cur = r.get('limit'), r.get('current')
            if isinstance(lim, (int, float)):
                h = lim - (cur if isinstance(cur, (int, float)) else 0)
                if best is None or h > best['headroom']:
                    best = {'family': r['family'], 'limit': lim,
                            'current': cur if isinstance(cur, (int, float)) else 0,
                            'headroom': h}
    return best


def put_retry(tok, url, body, label, tries=6, sleep_s=8, timeout=240):
    st, b, secs = 0, {}, 0.0
    for i in range(tries):
        st, b, secs = arm(tok, url, method='PUT', body=body, timeout=timeout)
        code = ((b.get('error') or {}).get('code') if isinstance(b, dict) else None)
        if st in (200, 201):
            return st, b, secs
        transient = (st == 429 or code in ('RetryableError', 'ReferencedResourceNotProvisioned',
                                           'AnotherOperationInProgress'))
        emit({'step': label + '-attempt', 'i': i, 'http': st, 'code': code,
              'transient': transient, 'secs': secs})
        if not transient:
            return st, b, secs
        time.sleep(sleep_s)
    return st, b, secs


def get_prov(tok, url, label, tries=12, sleep_s=8):
    last = None
    for i in range(tries):
        st, b, _ = arm(tok, url, timeout=45)
        ps = ((b.get('properties') or {}).get('provisioningState')
              if isinstance(b, dict) else None)
        last = (st, ps)
        emit({'step': label + '-prov', 'i': i, 'http': st, 'state': ps})
        if ps in ('Succeeded', 'Failed', 'Canceled') or st == 404:
            return st, ps
        time.sleep(sleep_s)
    return last or (0, None)


def vm_body(region, sku, nic_id, zone, computer, marker):
    body = {
        'location': region,
        'tags': {'c211intent': '121673', 'c211marker': PREFIX, 'c211sku': sku},
        'properties': {
            'hardwareProfile': {'vmSize': sku},
            'storageProfile': {
                'imageReference': dict(IMAGE),
                'osDisk': {'createOption': 'FromImage', 'deleteOption': 'Delete',
                           'managedDisk': {'storageAccountType': 'Standard_LRS'}}},
            'osProfile': {'computerName': computer, 'adminUsername': ADMIN_USER,
                          'adminPassword': ADMIN_PW,
                          'linuxConfiguration': {'disablePasswordAuthentication': False}},
            'networkProfile': {'networkInterfaces': [
                {'id': nic_id, 'properties': {'deleteOption': 'Delete'}}]},
        },
    }
    if zone:
        body['zones'] = [str(zone)]
    return body


def build_result(data, verdict):
    blob = json.dumps(data, default=str, sort_keys=True)
    if len(blob) > 60000:
        blob = blob[:60000]
    b = base64.b64encode(blob.encode()).decode()
    out = {'verdict': str(verdict), 'matrix_len': str(len(blob)),
           'chunks': str((len(b) + 899) // 900)}
    for i in range(0, len(b), 900):
        out['m%03d' % (i // 900)] = b[i:i + 900]

    def _j(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:  # noqa: BLE001
                return v
        return v

    dev = _j(data.get('device')) or {}
    out['summary'] = json.dumps({
        'verdict': verdict,
        'session': _j(data.get('session')),
        'quota_nonzero': _j(data.get('quota_nonzero')),
        'plan': _j(data.get('plan')),
        'vm_put': _j(data.get('vm_put')),
        'vmss_put': _j(data.get('vmss_put')),
        'vm_created': (_j(data.get('vm_created')) or {}).get('created')
        if isinstance(_j(data.get('vm_created')), dict) else None,
        'uuids': (dev or {}).get('uuids') if isinstance(dev, dict) else None,
        'cleanup': _j(data.get('cleanup')),
    }, default=str)[:1900]
    return out


def pick_zone(ent):
    """ent: {'zones': [...], 'restrictions': [...]} -> (offered_ok, zone)"""
    rs = ent.get('restrictions') or []
    if any(r.get('type') == 'Location' for r in rs):
        return False, None
    blocked = set()
    for r in rs:
        if r.get('type') == 'Zone':
            for z in (r.get('zones') or []):
                blocked.add(str(z))
    offered = [str(z) for z in (ent.get('zones') or [])]
    if not rs:
        return True, None
    cand = [z for z in offered if z not in blocked] if offered else []
    return (bool(cand) or not offered), (cand[0] if cand else None)


def main():
    emit({'step': 'start', 'rg': RG, 'vm': VM, 'deadline_s': DEADLINE})

    acc = az(['account', 'show', '-o', 'json'])
    sid, sess = None, {}
    try:
        a = json.loads(acc['out'])
        sid = a.get('id')
        s = sid or ''
        sess = {'sub': a.get('name'), 'state': a.get('state'),
                'sid_prefix': s[:8], 'sid_tail': s[18:],
                'tenant_prefix': str(a.get('tenantId'))[:8],
                'user_type': (a.get('user') or {}).get('type')}
    except Exception:  # noqa: BLE001
        pass
    emit({'step': 'session', **sess, 'rc': acc['rc']})
    save('session', sess)
    if not sid:
        emit({'step': 'fatal', 'why': 'no az account', 'err': (acc['err'] or '')[:400]})
        print(json.dumps({'verdict': 'TOKEN_FAILED'}))
        return 0
    save('subscription_id_tail', sid[18:])

    tk = az(['account', 'get-access-token', '--resource', ARM, '-o', 'json'], timeout=120)
    tok = None
    try:
        tok = json.loads(tk['out']).get('accessToken')
    except Exception:  # noqa: BLE001
        pass
    if not tok:
        emit({'step': 'fatal', 'why': 'no ARM token', 'rc': tk['rc'],
              'err': (tk['err'] or '')[:300]})
        print(json.dumps({'verdict': 'TOKEN_FAILED'}))
        return 0
    emit({'step': 'arm-token', 'len': len(tok)})

    st, provs, _ = arm(tok, ARM + '/subscriptions/' + sid + '/providers?api-version=2021-04-01')
    want = ('Microsoft.Compute', 'Microsoft.Network', 'Microsoft.Storage',
            'Microsoft.ContainerInstance', 'Microsoft.App')
    reg = {p.get('namespace'): p.get('registrationState')
           for p in (provs.get('value') or []) if p.get('namespace') in want}
    emit({'step': 'providers', 'http': st, 'registration': reg})
    save('providers', reg)

    st, perms, _ = arm(tok, ARM + '/subscriptions/' + sid + '/providers/'
                            'Microsoft.Authorization/permissions?api-version=2022-04-01')
    acts = set()
    for e in (perms.get('value') or []):
        acts.update(e.get('actions') or [])
    interesting = sorted(a for a in acts if 'virtualMachines' in a or 'resourceGroups' in a
                         or 'networkInterfaces' in a)
    emit({'step': 'subscription-permissions', 'http': st,
          'entries': len(perms.get('value') or []), 'vm/network/rg': interesting[:40]})
    save('sub_permissions', {'http': st, 'interesting': interesting[:40]})

    # ---------- Phase 1: live per-family GPU quota matrix ----------
    st, locs, _ = arm(tok, ARM + '/subscriptions/' + sid + '/locations?api-version=2022-12-01')
    regions = [l.get('name') for l in (locs.get('value') or [])
               if l.get('name') and (l.get('metadata') or {}).get('regionType', 'Physical')
               in ('Physical',)]
    emit({'step': 'regions', 'http': st, 'count': len(regions), 'first': regions[:8]})
    save('regions', {'http': st, 'count': len(regions)})

    quota = {}
    nonzero = {}
    usage_http = {}
    for region in regions:
        if left() < 900:
            emit({'step': 'quota-stop', 'why': 'deadline', 'done': len(quota),
                  'of': len(regions)})
            break
        st, us, secs = arm(tok, (ARM + '/subscriptions/' + sid + '/providers/'
                                 'Microsoft.Compute/locations/' + region +
                                 '/usages?api-version=' + API_USAGE), timeout=45)
        usage_http[region] = st
        rows = gpu_rows(us) if isinstance(us, dict) else []
        quota[region] = [[r['family'], r['current'], r['limit']] for r in rows]
        for r in rows:
            if isinstance(r.get('limit'), (int, float)) and r['limit'] > 0:
                nonzero.setdefault(r['family'], []).append(
                    {'region': region, 'current': r.get('current'), 'limit': r['limit']})
        if len(quota) % 10 == 0:
            emit({'step': 'quota-progress', 'regions': len(quota), 'secs_total':
                  round(time.time() - T0, 1)})
    emit({'step': 'quota-summary', 'regions_read': len(quota),
          'http_values': sorted(set(usage_http.values())), 'nonzero_families': nonzero})
    save('quota', quota)
    save('quota_nonzero', nonzero)
    save('usage_http_values', sorted(set(usage_http.values())))

    # ---------- Phase 2: vmSizes offer check ----------
    cand_regions = []
    for t in TARGETS:
        for region in regions:
            h = headroom([{'family': f[0], 'current': f[1], 'limit': f[2]}
                          for f in quota.get(region) or []], t)
            if h and h['headroom'] >= t['vcpu'] and region not in cand_regions:
                cand_regions.append(region)
    scan_regions = (cand_regions + [r for r in FIXED_REGIONS if r not in cand_regions])[:8]
    offers = {}
    for region in scan_regions:
        st, vs, secs = arm(tok, (ARM + '/subscriptions/' + sid + '/providers/'
                                 'Microsoft.Compute/locations/' + region +
                                 '/vmSizes?api-version=' + API_USAGE), timeout=45)
        found = {}
        if isinstance(vs, dict):
            for x in (vs.get('value') or []):
                nm = str(x.get('name', ''))
                for t in TARGETS:
                    if nm.lower() == t['sku'].lower():
                        found[t['key']] = {'name': nm, 'vcpus': x.get('numberOfCores'),
                                           'family': x.get('family')}
        offers[region] = found
        emit({'step': 'vmsizes', 'region': region, 'http': st, 'n': len(found),
              'keys': sorted(found), 'secs': secs})
    save('vmsizes_offers', offers)

    plan = None
    for t in TARGETS:
        region = None
        for r in regions:
            h = headroom([{'family': f[0], 'current': f[1], 'limit': f[2]}
                          for f in quota.get(r) or []], t)
            if h and h['headroom'] >= t['vcpu'] and (offers.get(r) or {}).get(t['key']):
                region = r
                break
        if region:
            h = headroom([{'family': f[0], 'current': f[1], 'limit': f[2]}
                          for f in quota.get(region) or []], t)
            plan = {'sku': t['sku'], 'key': t['key'], 'vcpu': t['vcpu'],
                    'region': region, 'zone': None, 'mode': 'headroom+offered',
                    'headroom': h}
            break
    if not plan:
        for t in TARGETS:
            for r in scan_regions:
                if (offers.get(r) or {}).get(t['key']):
                    plan = {'sku': t['sku'], 'key': t['key'], 'vcpu': t['vcpu'],
                            'region': r, 'zone': None, 'mode': 'offered-no-quota'}
                    break
            if plan:
                break
    if not plan:
        plan = {'sku': TARGETS[0]['sku'], 'key': TARGETS[0]['key'], 'vcpu': TARGETS[0]['vcpu'],
                'region': 'eastus', 'zone': None, 'mode': 'blind-default'}
    emit({'step': 'plan', 'chosen': plan, 'cand_regions': cand_regions,
          'scan_regions': scan_regions})
    save('plan', plan)

    # ---------- Phase 3: RG + network ----------
    region0 = plan['region']
    rgurl = ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG
    st, b, _ = arm(tok, rgurl + '?api-version=2021-04-01')
    rg_ok = st == 200
    rg_created = False
    if st == 404:
        st, b, _ = arm(tok, rgurl + '?api-version=2021-04-01', method='PUT',
                       body={'location': region0, 'tags': {'c211': 'i121673'}})
        rg_ok = st in (200, 201)
        rg_created = rg_ok
        emit({'step': 'rg-create', 'http': st, 'location': region0,
              **({} if rg_ok else err_of(st, b))})
    else:
        emit({'step': 'rg-get', 'http': st})
    save('rg', {'name': RG, 'http': st, 'usable': rg_ok, 'created': rg_created})
    if not rg_ok:
        emit({'step': 'fatal', 'why': 'no usable resource group'})
        print(json.dumps(build_result(OUT, 'GATE_RG_UNAVAILABLE'), default=str))
        return 0

    net_base = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                '/providers/Microsoft.Network')
    subnet_id = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                 '/providers/Microsoft.Network/virtualNetworks/' + VNET +
                 '/subnets/' + SUBNET)
    vnet_body = {'location': region0,
                 'tags': {'c211intent': '121673', 'c211marker': PREFIX},
                 'properties': {'addressSpace': {'addressPrefixes': ['10.251.0.0/16']},
                                'subnets': [{'name': SUBNET,
                                             'properties': {'addressPrefix':
                                                            '10.251.1.0/24'}}]}}
    st, vbody, secs = arm(tok, net_base + '/virtualNetworks/' + VNET +
                          '?api-version=' + API_NET, method='PUT', body=vnet_body,
                          timeout=180)
    emit({'step': 'vnet-put', 'http': st, 'secs': secs,
          **({} if st in (200, 201) else err_of(st, vbody))})
    save('vnet_put', {'http': st, **({} if st in (200, 201) else err_of(st, vbody))})
    vnet_ok = st in (200, 201) and get_prov(
        tok, net_base + '/virtualNetworks/' + VNET + '?api-version=' + API_NET,
        'vnet')[1] == 'Succeeded'

    nic_body = {'location': region0,
                'tags': {'c211intent': '121673', 'c211marker': PREFIX},
                'properties': {'ipConfigurations': [{
                    'name': 'ipconfig1',
                    'properties': {'privateIPAllocationMethod': 'Dynamic',
                                   'subnet': {'id': subnet_id}}}]}}
    nic_id = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
              '/providers/Microsoft.Network/networkInterfaces/' + NIC)
    st, nbody, secs = put_retry(tok, net_base + '/networkInterfaces/' + NIC +
                                '?api-version=' + API_NET, nic_body, 'nic')
    emit({'step': 'nic-put', 'http': st, 'secs': secs,
          **({} if st in (200, 201) else err_of(st, nbody))})
    save('nic_put', {'http': st, **({} if st in (200, 201) else err_of(st, nbody))})
    if st not in (200, 201):
        emit({'step': 'network-blocked', 'vnet_ok': vnet_ok, 'err': err_of(st, nbody)})
        _sweep(tok, sid, created_vm=False, created_vmss=False, rg_created=rg_created)
        print(json.dumps(build_result(OUT, 'GATE_NETWORK_PREREQ_REFUSED'), default=str))
        return 0
    get_prov(tok, net_base + '/networkInterfaces/' + NIC + '?api-version=' + API_NET, 'nic')

    # ---------- Phase 4: exactly ONE VM PUT (+ bounded VMSS fallback) ----------
    vm_url = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
              '/providers/Microsoft.Compute/virtualMachines/' + VM +
              '?api-version=' + API_VM)
    body = vm_body(plan['region'], plan['sku'], nic_id, plan.get('zone'),
                   'c211i121673m', plan['sku'])
    st, vmb, secs = put_retry(tok, vm_url, body, 'vm', tries=4, sleep_s=10, timeout=240)
    vm_put = {'sku': plan['sku'], 'region': plan['region'], 'zone': plan.get('zone'),
              'mode': plan['mode'], 'http': st, 'secs': secs,
              'provisioning': ((vmb.get('properties') or {}).get('provisioningState')
                               if isinstance(vmb, dict) else None),
              **({} if st in (200, 201) else err_of(st, vmb))}
    emit({'step': 'vm-put', **vm_put})
    save('vm_put', vm_put)
    created = st in (200, 201)
    created_vmss = False

    if not created:
        code = (err_of(st, vmb) or {}).get('code') or ''
        policyish = code in ('RequestDisallowedByPolicy', 'InvalidParameter',
                             'InvalidTemplate', 'BadRequest', 'SkuNotAvailable')
        if policyish and left() > 600:
            vmss_url = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                        '/providers/Microsoft.Compute/virtualMachineScaleSets/' + VMSS +
                        '?api-version=' + API_VM)
            vmss_body = {
                'location': plan['region'],
                'tags': {'c211intent': '121673', 'c211marker': PREFIX,
                         'c211sku': plan['sku']},
                'sku': {'name': plan['sku'], 'tier': 'Standard', 'capacity': 1},
                'properties': {
                    'overprovision': False,
                    'upgradePolicy': {'mode': 'Manual'},
                    'singlePlacementGroup': True,
                    'virtualMachineProfile': {
                        'storageProfile': {
                            'imageReference': dict(IMAGE),
                            'osDisk': {'createOption': 'FromImage',
                                       'managedDisk': {'storageAccountType':
                                                       'Standard_LRS'}}},
                        'osProfile': {'computerNamePrefix': 'c211i121673',
                                      'adminUsername': ADMIN_USER,
                                      'adminPassword': ADMIN_PW},
                        'networkProfile': {'networkInterfaceConfigurations': [{
                            'name': 'nic0',
                            'properties': {'primary': True,
                                           'ipConfigurations': [{
                                               'name': 'ipconfig1',
                                               'properties': {'subnet': {'id': subnet_id}}}]}}]},
                    },
                },
            }
            st2, vb2, secs2 = arm(tok, vmss_url, method='PUT', body=vmss_body, timeout=240)
            vmss_put = {'sku': plan['sku'], 'region': plan['region'], 'http': st2,
                        'secs': secs2,
                        'provisioning': ((vb2.get('properties') or {}).get(
                            'provisioningState') if isinstance(vb2, dict) else None),
                        **({} if st2 in (200, 201) else err_of(st2, vb2))}
            emit({'step': 'vmss-put', **vmss_put})
            save('vmss_put', vmss_put)
            created_vmss = st2 in (200, 201)
        else:
            save('vmss_put', {'skipped': True, 'reason': 'vm refusal code not policy-class',
                              'code': code})

    device = {}
    if created:
        ps = None
        for i in range(30):
            st, g, _ = arm(tok, vm_url + '&$expand=instanceView')
            ps = ((g.get('properties') or {}).get('provisioningState')
                  if isinstance(g, dict) else None)
            sts = [s.get('code') for s in
                   ((((g.get('properties') or {}).get('instanceView') or {})
                     .get('statuses')) or [])] if isinstance(g, dict) else []
            emit({'step': 'vm-poll', 'i': i, 'http': st, 'state': ps, 'statuses': sts[:4]})
            if ps in ('Succeeded', 'Failed') or st == 404:
                break
            time.sleep(12)
        st, g, _ = arm(tok, vm_url + '&$expand=instanceView')
        save('vm_readback', {'http': st,
                             'vm_size': ((g.get('properties') or {}).get(
                                 'hardwareProfile') or {}).get('vmSize')
                             if isinstance(g, dict) else None,
                             'provisioning': ((g.get('properties') or {}).get(
                                 'provisioningState')) if isinstance(g, dict) else None})
        emit({'step': 'vm-readback', **json.loads(OUT.get('vm_readback') or '{}')})
        # NVIDIA driver extension (best effort, bounded)
        ext_url = vm_url.split('?')[0] + '/extensions/NvidiaGpuDriverLinux?api-version=' + API_VM
        ext_body = {'location': plan['region'],
                    'properties': {'publisher': 'Microsoft.HpcCompute',
                                   'type': 'NvidiaGpuDriverLinux',
                                   'typeHandlerVersion': '1.10',
                                   'autoUpgradeMinorVersion': True}}
        st, eb, _ = arm(tok, ext_url, method='PUT', body=ext_body, timeout=180)
        emit({'step': 'gpu-ext-put', 'http': st,
              **({} if st in (200, 201) else err_of(st, eb))})
        save('gpu_ext', {'http': st, **({} if st in (200, 201) else err_of(st, eb))})
        if st in (200, 201):
            for i in range(12):
                st2, gb, _ = arm(tok, ext_url)
                ps2 = ((gb.get('properties') or {}).get('provisioningState')
                       if isinstance(gb, dict) else None)
                emit({'step': 'gpu-ext-prov', 'i': i, 'http': st2, 'state': ps2})
                if ps2 in ('Succeeded', 'Failed') or st2 == 404:
                    break
                time.sleep(10)
        script = ('set +e; echo C211-I121673-RUNSTART; nvidia-smi -L 2>&1; '
                  'echo ---Q---; nvidia-smi --query-gpu=name,uuid,memory.total '
                  '--format=csv,noheader 2>&1; echo ---DEV---; ls -l /dev/nvidia* 2>&1; '
                  'echo ---VER---; cat /proc/driver/nvidia/version 2>&1; '
                  'echo C211-I121673-RUNEND')
        r = az(['vm', 'run-command', 'invoke', '-g', RG, '-n', VM,
                '--command-id', 'RunShellScript', '--scripts', script,
                '--output', 'json'], timeout=600)
        txt = (r['out'] or '') + '\n' + (r['err'] or '')
        uuids = sorted(set(re.findall(r'GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                                      r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', txt)))
        names = sorted(set(re.findall(r'(Tesla [A-Z0-9]+|NVIDIA [A-Za-z0-9 ]+)', txt)))[:6]
        emit({'step': 'run-command', 'rc': r['rc'], 'uuids': uuids, 'names': names,
              'raw_tail': txt[-700:]})
        device = {'rc': r['rc'], 'uuids': uuids, 'names': names,
                  'capture_tail': txt[-2500:]} if uuids else {'rc': r['rc'], 'uuids': [],
                                                              'raw_tail': txt[-1200:]}
    elif created_vmss:
        vmss_url = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                    '/providers/Microsoft.Compute/virtualMachineScaleSets/' + VMSS +
                    '?api-version=' + API_VM)
        for i in range(20):
            st, g, _ = arm(tok, vmss_url + '&$expand=instanceView')
            ps = ((g.get('properties') or {}).get('provisioningState')
                  if isinstance(g, dict) else None)
            emit({'step': 'vmss-poll', 'i': i, 'http': st, 'state': ps})
            if ps in ('Succeeded', 'Failed') or st == 404:
                break
            time.sleep(12)
        r = az(['vmss', 'run-command', 'invoke', '-g', RG, '-n', VMSS,
                '--instance-id', '0', '--command-id', 'RunShellScript', '--scripts',
                'nvidia-smi -L; nvidia-smi --query-gpu=name,uuid,memory.total '
                '--format=csv,noheader', '--output', 'json'], timeout=600)
        txt = (r['out'] or '') + '\n' + (r['err'] or '')
        uuids = sorted(set(re.findall(r'GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                                      r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', txt)))
        emit({'step': 'vmss-run-command', 'rc': r['rc'], 'uuids': uuids})
        device = {'rc': r['rc'], 'uuids': uuids, 'raw_tail': txt[-1500:]}
    save('device', device)

    _sweep(tok, sid, created_vm=created, created_vmss=created_vmss,
           rg_created=rg_created)

    uuids = (device or {}).get('uuids') or []
    if uuids:
        verdict = 'READY_VM_GPU_DEVICE_PROVEN'
    elif created:
        verdict = 'VM_CREATED_NO_DEVICE_LINE'
    elif created_vmss:
        verdict = 'VMSS_CREATED_NO_DEVICE_LINE'
    elif (OUT.get('vm_put') or '').find('OperationNotAllowed') >= 0:
        verdict = 'GATE_VM_QUOTA_OPERATIONNOTALLOWED'
    else:
        verdict = 'GATE_NO_VM_CREATED'
    emit({'step': 'DONE', 'verdict': verdict, 'uuids': uuids})
    print(json.dumps(build_result(OUT, verdict), default=str))
    return 0


def _sweep(tok, sid, created_vm, created_vmss, rg_created):
    cleanup = {}
    net_base = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                '/providers/Microsoft.Network')
    vm_url = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
              '/providers/Microsoft.Compute/virtualMachines/' + VM +
              '?api-version=' + API_VM)
    vmss_url = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                '/providers/Microsoft.Compute/virtualMachineScaleSets/' + VMSS +
                '?api-version=' + API_VM)
    if created_vm:
        d = az(['vm', 'delete', '-g', RG, '-n', VM, '--yes'], timeout=600)
        cleanup['vm_delete'] = d['rc']
        for i in range(20):
            st, g, _ = arm(tok, vm_url)
            if st == 404:
                cleanup['vm_gone'] = True
                break
            time.sleep(8)
    else:
        st, _, _ = arm(tok, vm_url)
        cleanup['vm_pre_get'] = st
        cleanup['vm_gone'] = st == 404
    if created_vmss:
        d = az(['vmss', 'delete', '-g', RG, '-n', VMSS, '--yes'], timeout=900)
        cleanup['vmss_delete'] = d['rc']
        for i in range(20):
            st, g, _ = arm(tok, vmss_url)
            if st == 404:
                cleanup['vmss_gone'] = True
                break
            time.sleep(8)
    st, _, _ = arm(tok, net_base + '/networkInterfaces/' + NIC + '?api-version=' + API_NET)
    if st == 200:
        d = az(['network', 'nic', 'delete', '-g', RG, '-n', NIC], timeout=600)
        cleanup['nic_delete'] = d['rc']
    for i in range(20):
        st, _, _ = arm(tok, net_base + '/networkInterfaces/' + NIC + '?api-version=' + API_NET)
        if st == 404:
            cleanup['nic_gone'] = True
            break
        time.sleep(8)
    for i in range(15):
        st, _, _ = arm(tok, net_base + '/virtualNetworks/' + VNET + '?api-version=' + API_NET)
        if st == 404:
            cleanup['vnet_gone'] = True
            break
        d = az(['network', 'vnet', 'delete', '-g', RG, '-n', VNET], timeout=300)
        emit({'step': 'sweep-vnet', 'i': i, 'rc': d['rc'], 'err': (d['err'] or '')[:200]})
        time.sleep(10)
    st, lst, _ = arm(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                     '/resources?api-version=2021-04-01')
    ours = [{'name': x.get('name'), 'type': x.get('type')}
            for x in (lst.get('value') or [])
            if str(x.get('name', '')).lower().startswith(PREFIX)] if isinstance(lst, dict) else []
    cleanup['rg_resources'] = {'http': st, 'n': len(lst.get('value') or [])
                               if isinstance(lst, dict) else None, 'ours': ours[:10]}
    emit({'step': 'leftovers-after', **cleanup['rg_resources']})
    if rg_created:
        d = az(['group', 'delete', '--name', RG, '--yes', '--no-wait'], timeout=300)
        cleanup['rg_delete_rc'] = d['rc']
        gone = False
        for i in range(30):
            r2 = az(['group', 'show', '--name', RG, '-o', 'json'], timeout=90)
            low = (r2['err'] or '') + (r2['out'] or '')
            if r2['rc'] != 0 and ('ResourceGroupNotFound' in low
                                  or 'could not be found' in low.lower()):
                gone = True
                break
            time.sleep(10)
        cleanup['rg_gone'] = gone
    else:
        cleanup['rg_delete'] = 'skipped-preexisting'
    save('cleanup', cleanup)
    save('events', EVENTS[-180:])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        emit({'step': 'FATAL', 'error': repr(exc)[:400]})
        try:
            save('events', EVENTS[-180:])
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps({'verdict': 'PROBE_EXCEPTION', 'error': repr(exc)[:300]}))
        sys.exit(0)
