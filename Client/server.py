from flask import Flask, render_template, jsonify, request
import socket
import sounddevice as sd
import threading
import numpy as np
import time

app = Flask(__name__)

# Dynamic configuration with thread safety (shared settings)
CONFIG = {
    "host": "192.168.1.11",
    "port": 5000,
    "threshold": 300,
    "hangover_ms": 500,
    "blocksize": 1024,
    "samplerate": 16000
}

# Real-time state tracking for visualization on frontend
STATE = {
    "running": False,
    "status_message": "Disconnected",
    "current_rms": 0,
    "voice_active": False,
    "hangover_blocks_remaining": 0
}

sock = None
stream = None
lock = threading.Lock()

def audio_callback(indata, frames, time_info, status):
    global sock, STATE, CONFIG
    
    if not STATE["running"]:
        return

    try:
        # Convert raw bytes to numpy array to check volume level
        audio_data = np.frombuffer(indata, dtype=np.int16)
        
        # Calculate Root Mean Square (RMS) energy
        rms = np.sqrt(np.mean(np.square(audio_data.astype(np.float32))))
        STATE["current_rms"] = int(rms)
        
        # Calculate how many blocks represent the hangover duration
        # block duration (sec) = blocksize / samplerate
        block_duration_ms = (CONFIG["blocksize"] / CONFIG["samplerate"]) * 1000
        hangover_blocks = int(CONFIG["hangover_ms"] / block_duration_ms)
        
        if rms > CONFIG["threshold"]:
            # Voice detected: send the actual audio
            STATE["voice_active"] = True
            STATE["hangover_blocks_remaining"] = hangover_blocks
            if sock:
                sock.sendall(bytes(indata))
        else:
            if STATE["hangover_blocks_remaining"] > 0:
                # Keep streaming actual audio during hangover period
                STATE["voice_active"] = True
                STATE["hangover_blocks_remaining"] -= 1
                if sock:
                    sock.sendall(bytes(indata))
            else:
                # Background noise: send silence (zeros) to keep stream alive
                STATE["voice_active"] = False
                silence = np.zeros_like(audio_data)
                if sock:
                    sock.sendall(bytes(silence))

    except Exception as e:
        print("Send error in callback:", e)
        # We don't want to crash the callback thread but we want to report error
        STATE["status_message"] = f"Transmission Error: {str(e)}"
        # Trigger stopping from a separate thread to avoid deadlock in callback
        threading.Thread(target=stop_audio).start()


def start_audio():
    global sock, stream, STATE, CONFIG, lock

    with lock:
        if STATE["running"]:
            return

        try:
            STATE["status_message"] = f"Connecting to {CONFIG['host']}:{CONFIG['port']}..."
            print(STATE["status_message"])

            # Create TCP socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)  # Stop blocking indefinitely
            sock.connect((CONFIG["host"], CONFIG["port"]))
            sock.settimeout(None)  # Set back to blocking mode for continuous streaming

            STATE["running"] = True
            STATE["status_message"] = "Connected & Streaming"
            STATE["voice_active"] = False
            STATE["hangover_blocks_remaining"] = 0

            stream = sd.RawInputStream(
                samplerate=CONFIG["samplerate"],
                channels=1,
                dtype='int16',
                blocksize=CONFIG["blocksize"],
                callback=audio_callback
            )

            stream.start()
            print("🎤 Streaming started successfully")

        except Exception as e:
            print("Connection failed:", e)
            STATE["status_message"] = f"Connection Failed: {str(e)}"
            STATE["running"] = False
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None


def stop_audio():
    global STATE, stream, sock, lock

    with lock:
        if not STATE["running"] and stream is None and sock is None:
            return

        STATE["running"] = False
        STATE["voice_active"] = False
        STATE["current_rms"] = 0
        STATE["status_message"] = "Stopping stream..."

        try:
            if stream:
                if stream.active:
                    stream.stop()
                stream.close()
            
            if sock:
                sock.close()
        except Exception as e:
            print("Error during shutdown:", e)
        finally:
            stream = None
            sock = None
            STATE["status_message"] = "Disconnected"
            print("Streaming stopped and cleaned up")


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/start", methods=["GET", "POST"])
def start():
    global CONFIG, STATE
    
    if STATE["running"]:
        return jsonify({"status": "already_running", "message": "Streaming is already active"})

    # Parse and update dynamic config if provided in request (JSON or Form or Query Args)
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form or {}
    else:
        data = request.args

    if "host" in data and data["host"]:
        CONFIG["host"] = str(data["host"]).strip()
    if "port" in data and data["port"]:
        try:
            CONFIG["port"] = int(data["port"])
        except ValueError:
            pass
    if "threshold" in data and data["threshold"]:
        try:
            CONFIG["threshold"] = int(data["threshold"])
        except ValueError:
            pass
    if "hangover_ms" in data and data["hangover_ms"]:
        try:
            CONFIG["hangover_ms"] = int(data["hangover_ms"])
        except ValueError:
            pass

    # Start audio streaming in a background thread
    threading.Thread(target=start_audio).start()
    return jsonify({"status": "starting", "message": "Initiating connection..."})


@app.route("/stop", methods=["GET", "POST"])
def stop():
    stop_audio()
    return jsonify({"status": "stopped", "message": "Streaming stopped"})


@app.route("/status", methods=["GET"])
def get_status():
    global STATE, CONFIG
    return jsonify({
        "running": STATE["running"],
        "status_message": STATE["status_message"],
        "current_rms": STATE["current_rms"],
        "voice_active": STATE["voice_active"],
        "host": CONFIG["host"],
        "port": CONFIG["port"],
        "threshold": CONFIG["threshold"],
        "hangover_ms": CONFIG["hangover_ms"]
    })


@app.route("/update_settings", methods=["GET", "POST"])
def update_settings():
    global CONFIG
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form or {}
    else:
        data = request.args
    
    updated = []
    if "threshold" in data and data["threshold"] is not None:
        try:
            CONFIG["threshold"] = int(data["threshold"])
            updated.append("threshold")
        except ValueError:
            pass
            
    if "hangover_ms" in data and data["hangover_ms"] is not None:
        try:
            CONFIG["hangover_ms"] = int(data["hangover_ms"])
            updated.append("hangover_ms")
        except ValueError:
            pass

    return jsonify({
        "status": "updated",
        "message": f"Updated settings: {', '.join(updated)}",
        "threshold": CONFIG["threshold"],
        "hangover_ms": CONFIG["hangover_ms"]
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )