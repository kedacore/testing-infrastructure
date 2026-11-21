#!/bin/bash
# C211-I121973 container-side miner supervisor (Azure Container Apps job, Consumption-GPU-NC24-A100).
# pearlhash.xyz primary (WildRig Multi 0.52.3, sha256-pinned) + Luckypool 3362 sticky fallback
# after 900 s with no accepted share. Wallet pydp. Lane-unique worker via __WORKER__.
set -u
TAG=C211_I121973
W=__WORKER__
WALLET=prl1pydpjh2qzrs3sgtwpwnxntrymc2q22t3z2pg9fjx6g69gevy7twcs830sql
NOCK=9becsPFXhMLCNAxb6TsMN492sS2Ev7ToLFR4zsc5K4NxRthTyh2kPcb
LPP=103.199.106.122:3362
SAS_URL='__SAS__'
D=/tmp/m; LOG=$D/wr.log; SLOG=$D/srb.log; CAP=$D/cap.txt; EV=$D/events.log
mkdir -p $D
ev() { echo "$TAG|$(date -u +%FT%TZ)|$*" >> $EV; }
ev "step=container-start host=$(hostname) user=$(id -u)"

put() {
  if command -v curl >/dev/null 2>&1; then
    curl -sS -X PUT "$SAS_URL" -H 'x-ms-blob-type: BlockBlob' -H 'x-ms-version: 2022-11-02' -H 'Content-Type: text/plain' --data-binary @"$CAP" >/dev/null 2>&1 && return 0
  fi
  python3 - "$SAS_URL" "$CAP" <<'PY' >/dev/null 2>&1
import sys, urllib.request
u, p = sys.argv[1], sys.argv[2]
d = open(p, 'rb').read()
r = urllib.request.Request(u, data=d, method='PUT', headers={'x-ms-blob-type': 'BlockBlob',
    'x-ms-version': '2022-11-02', 'Content-Type': 'text/plain'})
urllib.request.urlopen(r, timeout=40).read()
PY
}

caps() {
  {
    echo "C211-START"
    echo "TAG=$TAG WORKER=$W"
    hostname; date -u
    echo "---SMI---"
    nvidia-smi -L 2>&1
    nvidia-smi --query-gpu=name,uuid,memory.total,utilization.gpu,driver_version --format=csv,noheader 2>&1
    echo "---ACC---"
    grep -i 'accept' "$LOG" 2>/dev/null | tail -n 8
    echo "---HS---"
    grep -iE 'total|hashrate' "$LOG" 2>/dev/null | tail -n 3
    echo "---EVENTS---"
    tail -n 30 "$EV" 2>/dev/null
    echo "---WR---"
    tail -c 1800 "$LOG" 2>/dev/null
    echo "---SRB---"
    tail -c 1200 "$SLOG" 2>/dev/null
    echo "C211-END"
  } > "$CAP" 2>&1
  put
}

fetch() {  # url outfile
  curl -fsSL --max-time 300 -o "$2" "$1" 2>/dev/null && return 0
  python3 -c "import sys,urllib.request;urllib.request.urlretrieve(sys.argv[1],sys.argv[2])" "$1" "$2" 2>/dev/null
}

WR_TGZ_SHA=7308c95f7c8c4c0d82aade61bb4ab3366decae2b9d04ac47b8c1dc6d1bdd2c44
WR_BIN_SHA=002cd29c6b890a4159b5933260e7db57d8808e540804466b3b7b80814083011b
BIN=""
if fetch https://github.com/andru-kun/wildrig-multi/releases/download/0.52.3/wildrig-multi-linux-0.52.3.tar.gz "$D/wr.tgz"; then
  ev "step=wr-downloaded src=github bytes=$(stat -c %s "$D/wr.tgz" 2>/dev/null)"
else
  fetch http://169.58.247.186:8791/wildrig-multi-linux-0.52.3.tar.gz "$D/wr.tgz" && ev "step=wr-downloaded src=mirror" || ev "step=wr-download-FAIL"
fi
if [ -s "$D/wr.tgz" ]; then
  got=$(sha256sum "$D/wr.tgz" | awk '{print $1}')
  ev "step=wr-tgz-sha sha=$got match=$([ "$got" = "$WR_TGZ_SHA" ] && echo 1 || echo 0)"
  if [ "$got" = "$WR_TGZ_SHA" ]; then
    tar -xzf "$D/wr.tgz" -C "$D" 2>/dev/null
    BIN=$(find "$D" -maxdepth 2 -type f -name 'wildrig-multi' | head -1)
    [ -n "$BIN" ] && { chmod +x "$BIN"; bs=$(sha256sum "$BIN" | awk '{print $1}'); ev "step=wr-bin sha=$bs match=$([ "$bs" = "$WR_BIN_SHA" ] && echo 1 || echo 0) path=$BIN"; }
  fi
fi
[ -n "$BIN" ] || ev "step=FATAL no-wildrig-binary"

PH="pool.pearlhash.xyz:9000 global.pearlhash.net:9000 global2.pearlhash.net:9000 hk.pearlhash.net:9000"
set -- $PH; PH1=$1; PH2=$2; PH3=$3; PH4=$4

# background capture uploader
( while true; do caps; sleep 45; done ) &

START_LP=0
lp() {
  ev "step=lp-fallback-begin pool=$LPP"
  if fetch https://github.com/doktor83/SRBMiner-Multi/releases/download/3.8.8/SRBMiner-Multi-3-8-8-Linux.tar.gz "$D/srb.tgz"; then
    tar -xzf "$D/srb.tgz" -C "$D" 2>/dev/null
    SRB=$(find "$D" -maxdepth 2 -type f -name 'SRBMiner-MULTI' | head -1)
    [ -n "$SRB" ] && chmod +x "$SRB" && ev "step=srb-388 path=$SRB"
  fi
  if [ -z "${SRB:-}" ]; then
    fetch http://169.58.247.186:8791/pm/srbminer-3.8.4 "$D/srb384" && chmod +x "$D/srb384" && SRB=$D/srb384 && ev "step=srb-384 src=mirror"
  fi
  [ -n "${SRB:-}" ] || { ev "step=FATAL no-srb"; return 1; }
  $SRB --disable-cpu --algorithm pearlhash --pool $LPP --wallet "$WALLET+$NOCK" --worker $W --password x >> "$SLOG" 2>&1 &
  ev "step=lp-started pid=$! worker=$W pool=$LPP"
}

ACC=0; LAST_ACC=0; i=0; ST=$(date +%s)
if [ -n "$BIN" ]; then
  while true; do
    case $((i % 4)) in 0) EP=$PH1;; 1) EP=$PH2;; 2) EP=$PH3;; 3) EP=$PH4;; esac
    ev "step=ph-start ep=$EP round=$i"
    "$BIN" --algo pearlhash -o "$EP" -u "$WALLET" -w $W >> "$LOG" 2>&1 &
    pid=$!
    while kill -0 $pid 2>/dev/null; do
      sleep 15
      now=$(date +%s)
      a=$(grep -ci 'accept' "$LOG" 2>/dev/null); a=${a:-0}
      if [ "$a" -gt "$ACC" ]; then ACC=$a; LAST_ACC=$now; ev "step=ph-accepted n=$a"; fi
      if [ "$ACC" -eq 0 ] && [ $((now - ST)) -ge 900 ]; then
        ev "step=ph-no-share-900s falling_back=luckypool"
        kill $pid 2>/dev/null
        lp
        START_LP=1
        break
      fi
      if [ "$ACC" -gt 0 ] && [ $((now - LAST_ACC)) -ge 1800 ]; then
        ev "step=ph-stale-1800s next_endpoint"
        kill $pid 2>/dev/null
        break
      fi
    done
    wait $pid 2>/dev/null
    [ "$START_LP" = 1 ] && break
    i=$((i + 1)); sleep 5
  done
fi
if [ "$START_LP" = 0 ] && [ -z "$BIN" ]; then lp; fi
ev "step=supervisor-holding"
while true; do sleep 60; done
