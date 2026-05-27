from flask import Flask, render_template
import socket
import sounddevice as sd
import threading
import numpy as np

app = Flask(__name__)

HOST = "192.168.1.11"      # Friend IP
PORT = 5000

sock = None
stream = None
running = False


def audio_callback(indata, frames, time, status):
    global sock, running

    if not running:
        return

    try:
        # Convert raw bytes to numpy array to check volume level
        audio_data = np.frombuffer(indata, dtype=np.int16)
        
        # Calculate Root Mean Square (RMS) energy
        rms = np.sqrt(np.mean(np.square(audio_data.astype(np.float32))))
        
        # Set a noise threshold (adjust this value depending on background noise level)
        NOISE_THRESHOLD = 300
        
        if rms > NOISE_THRESHOLD:
            # Voice detected: send the actual audio
            sock.sendall(bytes(indata))
        else:
            # Background noise detected: send silence (zeros) to keep stream alive
            silence = np.zeros_like(audio_data)
            sock.sendall(bytes(silence))

    except Exception as e:
        print("Send error:", e)


def start_audio():

    global sock
    global stream
    global running

    if running:
        return

    try:

        print("Connecting...")

        sock = socket.socket()
        sock.connect((HOST, PORT))

        running = True

        stream = sd.RawInputStream(
            samplerate=16000,
            channels=1,
            dtype='int16',
            blocksize=4096,
            callback=audio_callback
        )

        stream.start()

        print("🎤 Streaming started")

    except Exception as e:
        print(e)


def stop_audio():

    global running
    global stream
    global sock

    running = False

    try:

        if stream:

            if stream.active:
                stream.stop()

            stream.close()
            stream = None

        if sock:
            sock.close()
            sock = None

        print("Stopped")

    except Exception as e:
        print(e)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/start")
def start():

    threading.Thread(
        target=start_audio
    ).start()

    return "started"


@app.route("/stop")
def stop():

    stop_audio()

    return "stopped"


if __name__=="__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )