import socket
import threading
import wave
import os
import time
import math
import re
import warnings
from datetime import datetime

from elevenlabs.client import ElevenLabs
from elevenlabs.play import play
from deep_translator import GoogleTranslator

warnings.filterwarnings("ignore")

# --- Configuration ---
HOST = '[IP_ADDRESS]'
PORT = 5000
ELEVENLABS_API_KEY = "sk_4d3907e31d5b9e021913ab2c8d3f64932390ea41c05c78dd"
TARGET_LANGUAGE = 'ta'  # Target language code (e.g., 'ta' for Tamil)

SILENCE_THRESHOLD = 800
SILENCE_DURATION_LIMIT = 0.8
MAX_CHUNK_LIMIT = 320000 * 3  # Roughly 30 seconds

# Global locking for TTS playback to prevent audio overlap when multiple clients trigger TTS simultaneously
tts_lock = threading.Lock()

# Global session counter to give unique IDs to each connection
session_counter = 1
session_counter_lock = threading.Lock()

class AudioProcessor:
    """Handles audio buffer mathematics and WAV reconstruction."""
    
    @staticmethod
    def get_rms(data):
        """Calculates the Root Mean Square (RMS) energy to detect volume/silence."""
        if len(data) % 2 != 0:
            data = data[:-(len(data) % 2)]
        if not data: return 0
        try:
            mv = memoryview(data).cast('h')
            return math.sqrt(sum(int(x)*int(x) for x in mv) / len(mv))
        except Exception:
            return 0

    @staticmethod
    def save_wav(filename, audio_data):
        """Reconstructs raw PCM bytes into a valid WAV file."""
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(audio_data)

class PipelineManager:
    """Manages the full AI pipeline: Speech-to-Text -> Translation -> Text-to-Speech."""
    
    def __init__(self, api_key):
        self.client = ElevenLabs(api_key=api_key)
        self.translator = GoogleTranslator(source='auto', target=TARGET_LANGUAGE)
        
    def process(self, session_id, session_dir, audio_data, sequence_num):
        """Executes the complete pipeline asynchronously."""
        if len(audio_data) == 0:
            return

        # 1. Save original audio
        received_wav_path = os.path.join(session_dir, f"received_audio_{sequence_num}.wav")
        AudioProcessor.save_wav(received_wav_path, audio_data)

        try:
            # 2. STT (Speech to Text)
            with open(received_wav_path, "rb") as audio_file:
                transcript = self.client.speech_to_text.convert(
                    file=audio_file,
                    model_id="scribe_v2",
                    language_code="eng" # Locking to English to avoid foreign language hallucinations
                )
            
            text = transcript.text.strip() if transcript and getattr(transcript, 'text', None) else ""
            
            # Filter out non-speech tags common in Whisper/Scribe (e.g. [background noise])
            filtered_text = re.sub(r'\[.*?\]', '', text).strip()

            if not filtered_text:
                return

            print(f"[{session_id}] Speech recognized")
            print(f"[{session_id}] Original: {filtered_text}")

            # 3. Translation
            try:
                translated_text = self.translator.translate(filtered_text)
                print(f"[{session_id}] Translation completed")
                print(f"[{session_id}] Translated ({TARGET_LANGUAGE}): {translated_text}")
            except Exception as e:
                print(f"[{session_id}] Translation error: {e}")
                return

            # 4. Save Transcript Log
            transcript_path = os.path.join(session_dir, "transcript.txt")
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(transcript_path, "a", encoding="utf-8") as f:
                f.write(f"===== Speech Transcript =====\n")
                f.write(f"Session: {session_id}\n")
                f.write(f"Timestamp: {current_time}\n\n")
                f.write(f"Original:\n{filtered_text}\n\n")
                f.write(f"Translated ({TARGET_LANGUAGE}):\n{translated_text}\n")
                f.write("-" * 30 + "\n\n")

            # 5. TTS (Text to Speech)
            try:
                audio_stream = self.client.text_to_speech.convert(
                    voice_id="21m00Tcm4TlvDq8ikWAM",  # Rachel voice
                    optimize_streaming_latency=3,
                    text=translated_text,
                    model_id="eleven_turbo_v2_5" # Turbo model handles fast generation and multi-language
                )
                
                # Consume stream into memory so we can save it and play it
                audio_bytes = b"".join(list(audio_stream))
                translated_mp3_path = os.path.join(session_dir, f"translated_audio_{sequence_num}.mp3")
                
                with open(translated_mp3_path, "wb") as f:
                    f.write(audio_bytes)
                    
                print(f"[{session_id}] Voice generated")
                print(f"[{session_id}] Files saved")
                
                # 6. Audio Output (Local Playback)
                # The tts_lock ensures multiple clients don't output audio over each other simultaneously
                with tts_lock:
                    print(f"[{session_id}] Playing translated speech...")
                    play(audio_bytes)
                    
            except Exception as e:
                print(f"[{session_id}] TTS error: {e}")

        except Exception as e:
            print(f"[{session_id}] Pipeline error: {e}")


class ClientSession(threading.Thread):
    """Manages the network socket connection and silence detection for a single client."""
    
    def __init__(self, conn, addr, session_id, pipeline_manager):
        super().__init__()
        self.conn = conn
        self.addr = addr
        self.session_id = session_id
        self.pipeline_manager = pipeline_manager
        self.session_dir = os.path.join("sessions", self.session_id)
        self.sequence_num = 1
        
        # Isolate session artifacts into their own directory
        os.makedirs(self.session_dir, exist_ok=True)
        print(f"[{self.session_id}] Client connected: {self.session_id} from {self.addr}")

    def run(self):
        self.conn.settimeout(0.1)
        audio = b''
        last_speech_time = time.time()
        has_speech = False
        
        while True:
            try:
                try:
                    data = self.conn.recv(4096)
                except socket.timeout:
                    data = None

                # Detect clean disconnection
                if data == b'':
                    print(f"[{self.session_id}] Client disconnected")
                    if len(audio) > 0:
                        self.trigger_pipeline(audio)
                    break

                if data:
                    audio += data
                    rms = AudioProcessor.get_rms(data)
                    
                    # Voice Activity Detection
                    if rms > SILENCE_THRESHOLD:
                        last_speech_time = time.time()
                        has_speech = True

                # Determine if user finished sentence
                if has_speech and (time.time() - last_speech_time > SILENCE_DURATION_LIMIT):
                    self.trigger_pipeline(audio)
                    audio = b''
                    has_speech = False
                    last_speech_time = time.time()
                    
                # Hard limit to prevent infinite buffer growth
                if len(audio) >= MAX_CHUNK_LIMIT:
                    self.trigger_pipeline(audio)
                    audio = b''
                    has_speech = False
                    last_speech_time = time.time()

            except Exception as e:
                print(f"[{self.session_id}] Network error: {e}")
                if len(audio) > 0:
                    self.trigger_pipeline(audio)
                break
                
        self.conn.close()

    def trigger_pipeline(self, audio_data):
        if len(audio_data) == 0:
            return
        
        print(f"[{self.session_id}] Audio received")
        
        # Deep copy values so the receiving loop can instantly resume
        current_audio = audio_data
        current_seq = self.sequence_num
        self.sequence_num += 1
        
        # Offload AI operations to background thread to maintain realtime network buffering
        pipeline_thread = threading.Thread(
            target=self.pipeline_manager.process, 
            args=(self.session_id, self.session_dir, current_audio, current_seq)
        )
        pipeline_thread.daemon = True
        pipeline_thread.start()


class ReceiverServer:
    """Main TCP Server orchestrating client connections."""
    
    def __init__(self, host, port, api_key):
        self.host = host
        self.port = port
        self.pipeline_manager = PipelineManager(api_key)
        
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def start(self):
        global session_counter
        try:
            self.server.bind((self.host, self.port))
            self.server.listen(5)
            print(f"=== Audio Receiver Server ===")
            print(f"Listening on {self.host}:{self.port}")
            print(f"Translation Target: '{TARGET_LANGUAGE}'")
            print(f"Multi-client support: ENABLED")
            print(f"=============================")
            
            while True:
                conn, addr = self.server.accept()
                
                # Safely assign a new session ID
                with session_counter_lock:
                    session_id = f"session_{session_counter:03d}"
                    session_counter += 1
                
                # Spawn an isolated session thread
                client_thread = ClientSession(conn, addr, session_id, self.pipeline_manager)
                client_thread.daemon = True
                client_thread.start()
                
        except KeyboardInterrupt:
            print("\nServer shutting down gracefully.")
        except Exception as e:
            print(f"Fatal server error: {e}")
        finally:
            self.server.close()


if __name__ == "__main__":
    server = ReceiverServer(HOST, PORT, ELEVENLABS_API_KEY)
    server.start()
