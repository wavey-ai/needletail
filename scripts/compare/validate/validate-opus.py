import csv, struct, subprocess, numpy as np, re
def ogg_crc(data):
    crc = 0
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04c11db7) & 0xffffffff if crc & 0x80000000 else (crc << 1) & 0xffffffff
    return crc
def page(serial, seq, granule, packet, flags=0):
    segs = [255] * (len(packet) // 255) + [len(packet) % 255]
    head = b"OggS" + bytes([0, flags]) + struct.pack("<qIII", granule, serial, seq, 0) + bytes([len(segs)]) + bytes(segs)
    crc = ogg_crc(head + packet)
    return head[:22] + struct.pack("<I", crc) + head[26:] + packet
rows = list(csv.DictReader(open("opus-units/units.csv")))
parts = sorted((int(re.search(r"part(\d+)", r["uri"]).group(1)), open("opus-units/" + r["file"], "rb").read()) for r in rows)
print("parts", len(parts), "numbers", parts[0][0], "..", parts[-1][0], "size", len(parts[0][1]), "header", parts[0][1][:20].hex(), "toc", hex(parts[0][1][20]))
out = page(1, 0, 0, b"OpusHead" + bytes([1, 2]) + struct.pack("<HIh", 0, 48000, 0) + bytes([0]), 2)
out += page(1, 1, 0, b"OpusTags" + struct.pack("<I", 4) + b"test" + struct.pack("<I", 0))
for i, (number, body) in enumerate(parts):
    out += page(1, 2 + i, (i + 1) * 240, body[20:])
open("nt-opus.ogg", "wb").write(out)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "nt-opus.ogg", "-f", "s16le", "-ac", "2", "-ar", "48000", "nt-opus.pcm"], check=True)
pcm = np.fromfile("nt-opus.pcm", dtype=np.int16).reshape(-1, 2)[:, 0].astype(np.float64)
print("decoded samples", len(pcm), "expected", len(parts) * 240)
t = np.arange(len(pcm)) / 48000.0
win = 48
power = np.abs(np.convolve(pcm * np.exp(-2j * np.pi * 3000 * t), np.ones(win), "valid")) / win
total = np.sqrt(np.convolve(pcm * pcm, np.ones(win), "valid") / win) + 1
ratio = power / total
i = 0
first = parts[0][0]
while i < len(ratio):
    if ratio[i] > 0.45 and power[i] > 400:
        content = first * 240 + i - 512
        print("marker onset near decoded sample", i, "-> content sample", content, "; offset from a whole second:", (content + 24000) % 48000 - 24000, "samples =", round(((content + 24000) % 48000 - 24000) / 48, 2), "ms")
        i += 24000
    else:
        i += 1
