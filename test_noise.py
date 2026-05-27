import sounddevice as sd
import numpy as np

def audio_callback(indata, frames, time, status):
    rms = np.sqrt(np.mean(np.square(np.frombuffer(indata, dtype=np.int16).astype(np.float32))))
    print("RMS:", rms)
    raise sd.CallbackStop()

stream = sd.RawInputStream(samplerate=16000, channels=1, dtype='int16', blocksize=4096, callback=audio_callback)
with stream:
    sd.sleep(1000)
