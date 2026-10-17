"""Summarise a sidecar_host stdout capture: packet count, keyframes, bytes, fps, and dump the H.264 stream."""
import json
import struct
import sys

data = open(sys.argv[1], "rb").read()
off, pk, info = 0, [], None
es = bytearray()
while off + 13 <= len(data):
    n, fl, pts = struct.unpack_from(">IBQ", data, off)
    body = data[off + 13: off + 13 + n]
    off += 13 + n
    if fl & 0x80:
        info = json.loads(body)
        continue
    pk.append((fl, n, pts))
    es += body
print("info", info)
if pk:
    dur = (pk[-1][2] - pk[0][2]) / 1e6
    print(f"packets {len(pk)} keys {sum(f & 1 for f, *_ in pk)} bytes {sum(n for _, n, _ in pk)} span {dur:.2f}s "
          f"fps {len(pk) / dur if dur else 0:.1f}")
    print("first NAL types", [b & 0x1F for b in [es[i + 4] for i in range(len(es) - 4) if es[i:i + 4] == b"\0\0\0\1"][:8]])
if len(sys.argv) > 2:
    open(sys.argv[2], "wb").write(es)
