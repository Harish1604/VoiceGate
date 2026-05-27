import sounddevice as sd
import numpy as np

print("Testing Microphone Device None (Default Pipewire)...")
try:
    stream = sd.InputStream(device=None, channels=1, samplerate=16000, dtype='int16', blocksize=960)
    stream.start()
    for i in range(10):
        data, _ = stream.read(960)
        rms = np.sqrt(np.mean(np.square(data.astype(np.float32))))
        print(f"Chunk {i}: RMS = {int(rms)}")
    stream.stop()
    stream.close()
    print("Test complete.")
except Exception as e:
    print(f"Error: {e}")
