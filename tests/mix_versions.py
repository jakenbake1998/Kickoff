"""Song-mix check: a second mix of the song lines up with it (a shift), other songs of the same length and tempo do not (None)."""
import sys, os, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import make_synthetic as ms
from scipy.signal import butter, sosfilt
import musicsync as m
d = "/tmp/mixc"; os.makedirs(d, exist_ok=True)
song, _ = ms.make_master()
ms.write_wav(d + "/Song Master.wav", song)
v2 = sosfilt(butter(4, 2500, "low", fs=ms.FS, output="sos"), song) * 0.8
v2[int(30*ms.FS):int(40*ms.FS)] += 0.15*np.sin(2*np.pi*660*np.arange(int(10*ms.FS))/ms.FS)   # a new part
ms.write_wav(d + "/Song Master v2.wav", v2 / np.abs(v2).max() * 0.9)
parts = [ms.section(4, 11), ms.section(8, 12), ms.section(8, 13), ms.section(8, 14), ms.section(8, 13), ms.section(9, 15)]
other = np.concatenate(parts); ms.write_wav(d + "/Other Song.wav", other / np.abs(other).max() * 0.9)
o2 = np.concatenate([ms.section(4, 21, 132), ms.section(8, 22, 132), ms.section(8, 23, 132), ms.section(8, 24, 132), ms.section(8, 23, 132), ms.section(10, 25, 132)])
ms.write_wav(d + "/Faster Song.wav", o2 / np.abs(o2).max() * 0.9)
for f in ["Song Master v2.wav", "Other Song.wav", "Faster Song.wav"]:
    print(f, m.song_shift(d + "/Song Master.wav", d + "/" + f))
