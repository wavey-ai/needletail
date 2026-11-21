import numpy as np, csv, re, sys
name, first, offset = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
pcm = np.fromfile(name, dtype=np.int16).reshape(-1, 2)[:, 0].astype(np.float64)
t = np.arange(len(pcm)) / 48000.0
env = np.abs(np.convolve(pcm * np.exp(-2j * np.pi * 3000 * t), np.ones(480), "valid")) / 480
n = (len(env) // 48000) * 48000
fold = env[:n].reshape(-1, 48000).sum(axis=0)
phi = int(np.argmax(fold))
content = first * 240 + phi - offset
delta = (content + 24000) % 48000 - 24000
print(name, "seconds", n // 48000, "burst starts at decoded sample phase", phi, "peak/median", round(fold[phi] / np.median(fold), 1),
      "-> content sample offset from a whole second:", delta, "samples =", round(delta / 48, 3), "ms")
