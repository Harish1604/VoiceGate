# =========================================================
# server.py
# =========================================================

import asyncio
import fractions
import io
import logging
import os
import re
import wave
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import av
import numpy as np
import webrtcvad

from aiohttp import web
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    MediaStreamTrack,
    MediaStreamError
)

from pydub import AudioSegment
from dotenv import load_dotenv

from elevenlabs.client import ElevenLabs
from deep_translator import GoogleTranslator

# =========================================================
# LOAD ENV
# =========================================================

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

HOST = os.getenv("HOST", "0.0.0.0")

PORT = int(
    os.getenv("PORT", 8080)
)

TARGET_LANGUAGE = os.getenv(
    "TARGET_LANGUAGE",
    "ta"
)

ELEVENLABS_API_KEY = os.getenv(
    "ELEVENLABS_API_KEY"
)

if not ELEVENLABS_API_KEY:
    raise RuntimeError(
        "ELEVENLABS_API_KEY not found in .env"
    )

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

FRAME_DURATION_MS = 30

FRAME_SIZE = int(
    SAMPLE_RATE *
    FRAME_DURATION_MS / 1000
) * SAMPLE_WIDTH

SILENCE_DURATION = 1.2

MAX_WORKERS = 4

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("VoxGate")

# =========================================================
# EXECUTOR
# =========================================================

executor = ThreadPoolExecutor(
    max_workers=MAX_WORKERS
)

pcs = set()

# =========================================================
# PIPELINE
# =========================================================

class PipelineManager:

    def __init__(self):

        self.client = ElevenLabs(
            api_key=ELEVENLABS_API_KEY
        )

        self.translator = GoogleTranslator(
            source="auto",
            target=TARGET_LANGUAGE
        )

    def process_audio(
        self,
        session_id,
        audio_bytes
    ):

        try:

            session_dir = (
                SESSIONS_DIR /
                session_id
            )

            session_dir.mkdir(
                exist_ok=True
            )

            wav_path = (
                session_dir /
                "temp.wav"
            )

            with wave.open(
                str(wav_path),
                'wb'
            ) as wf:

                wf.setnchannels(CHANNELS)
                wf.setsampwidth(SAMPLE_WIDTH)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(audio_bytes)

            # =====================================
            # STT
            # =====================================

            with open(wav_path, "rb") as f:

                transcript = (
                    self.client
                    .speech_to_text
                    .convert(
                        file=f,
                        model_id="scribe_v2",
                        language_code="eng"
                    )
                )

            text = (
                transcript.text.strip()
                if transcript and transcript.text
                else ""
            )

            text = re.sub(
                r'\[.*?\]',
                '',
                text
            ).strip()

            if not text:
                return None

            logger.info(
                f"[{session_id}] "
                f"Original: {text}"
            )

            # =====================================
            # TRANSLATION
            # =====================================

            translated = (
                self.translator.translate(text)
            )

            logger.info(
                f"[{session_id}] "
                f"Translated: {translated}"
            )

            # =====================================
            # SAVE TRANSCRIPT
            # =====================================

            transcript_path = (
                session_dir /
                "transcript.txt"
            )

            with open(
                transcript_path,
                "a",
                encoding="utf-8"
            ) as f:

                f.write(
                    f"Original: {text}\n"
                )

                f.write(
                    f"Translated: "
                    f"{translated}\n"
                )

                f.write(
                    "-" * 50 + "\n"
                )

            # =====================================
            # TTS
            # =====================================

            audio_stream = (
                self.client
                .text_to_speech
                .convert(
                    voice_id="21m00Tcm4TlvDq8ikWAM",
                    text=translated,
                    model_id="eleven_turbo_v2_5",
                    optimize_streaming_latency=3
                )
            )

            mp3_bytes = b''.join(audio_stream)

            return mp3_bytes

        except Exception as e:

            logger.error(
                f"[{session_id}] "
                f"Pipeline Error: {e}"
            )

            return None

pipeline = PipelineManager()

# =========================================================
# OUTGOING AUDIO TRACK
# =========================================================

class TranslatedAudioTrack(MediaStreamTrack):

    kind = "audio"

    def __init__(self):

        super().__init__()

        self.queue = asyncio.Queue()

    async def recv(self):

        pcm = await self.queue.get()

        frame = av.AudioFrame.from_ndarray(
            pcm,
            format="s16",
            layout="mono"
        )

        frame.sample_rate = SAMPLE_RATE

        frame.time_base = fractions.Fraction(
            1,
            SAMPLE_RATE
        )

        return frame

# =========================================================
# AUDIO RECEIVER
# =========================================================

class AudioReceiver:

    def __init__(
        self,
        track,
        outgoing_track,
        session_id
    ):

        self.track = track

        self.outgoing_track = outgoing_track

        self.session_id = session_id

        self.vad = webrtcvad.Vad(2)

        self.audio_buffer = bytearray()

        self.last_voice = (
            asyncio.get_event_loop().time()
        )

        self.has_voice = False

    async def start(self):

        try:
            while True:

                frame = await self.track.recv()

                pcm = frame.to_ndarray()

                audio_bytes = pcm.tobytes()

                self.audio_buffer.extend(audio_bytes)

                # =====================================
                # VAD
                # =====================================

                for i in range(
                    0,
                    len(audio_bytes) - FRAME_SIZE,
                    FRAME_SIZE
                ):

                    chunk = audio_bytes[
                        i:i + FRAME_SIZE
                    ]

                    try:

                        speech = (
                            self.vad.is_speech(
                                chunk,
                                SAMPLE_RATE
                            )
                        )

                        if speech:

                            self.has_voice = True

                            self.last_voice = (
                                asyncio
                                .get_event_loop()
                                .time()
                            )

                    except Exception:
                        pass

                # =====================================
                # SILENCE DETECT
                # =====================================

                if (
                    self.has_voice and
                    asyncio.get_event_loop().time()
                    - self.last_voice >
                    SILENCE_DURATION
                ):

                    logger.info(
                        f"[{self.session_id}] "
                        f"Processing audio chunk"
                    )

                    audio_copy = bytes(
                        self.audio_buffer
                    )

                    self.audio_buffer.clear()

                    self.has_voice = False

                    loop = asyncio.get_running_loop()

                    mp3_bytes = await loop.run_in_executor(
                        executor,
                        pipeline.process_audio,
                        self.session_id,
                        audio_copy
                    )

                    if mp3_bytes:

                        await self.send_audio_back(
                            mp3_bytes
                        )
        except MediaStreamError:
            logger.info(
                f"[{self.session_id}] "
                f"Audio track ended"
            )

    async def send_audio_back(
        self,
        mp3_bytes
    ):

        audio = AudioSegment.from_file(
            io.BytesIO(mp3_bytes),
            format="mp3"
        )

        audio = (
            audio
            .set_frame_rate(SAMPLE_RATE)
            .set_channels(1)
            .set_sample_width(2)
        )

        pcm = np.array(
            audio.get_array_of_samples(),
            dtype=np.int16
        )

        chunk_size = 960

        for i in range(
            0,
            len(pcm),
            chunk_size
        ):

            chunk = pcm[i:i + chunk_size]

            if len(chunk) < chunk_size:
                break

            await self.outgoing_track.queue.put(
                chunk.reshape(1, -1)
            )

# =========================================================
# WEBRTC SIGNALING
# =========================================================

async def offer(request):

    params = await request.json()

    offer = RTCSessionDescription(
        sdp=params["sdp"],
        type=params["type"]
    )

    pc = RTCPeerConnection()

    pcs.add(pc)

    session_id = f"session_{id(pc)}"

    logger.info(
        f"[{session_id}] Connected"
    )

    outgoing_track = TranslatedAudioTrack()

    pc.addTrack(outgoing_track)

    @pc.on("track")
    def on_track(track):

        if track.kind == "audio":

            logger.info(
                f"[{session_id}] "
                f"Audio track received"
            )

            receiver = AudioReceiver(
                track,
                outgoing_track,
                session_id
            )

            asyncio.create_task(
                receiver.start()
            )

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():

        logger.info(
            f"[{session_id}] "
            f"State: {pc.connectionState}"
        )

        if pc.connectionState in [
            "failed",
            "closed",
            "disconnected"
        ]:

            await pc.close()

            pcs.discard(pc)

    await pc.setRemoteDescription(
        offer
    )

    answer = await pc.createAnswer()

    await pc.setLocalDescription(
        answer
    )

    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })

# =========================================================
# CLEANUP
# =========================================================

async def on_shutdown(app):

    coros = [
        pc.close()
        for pc in pcs
    ]

    await asyncio.gather(*coros)

    pcs.clear()

# =========================================================
# MAIN
# =========================================================

app = web.Application()

app.router.add_post(
    "/offer",
    offer
)

app.on_shutdown.append(
    on_shutdown
)

if __name__ == "__main__":

    logger.info("=================================")

    logger.info(
        "VoxGate WebRTC Server Started"
    )

    logger.info(
        f"http://{HOST}:{PORT}"
    )

    logger.info("=================================")

    web.run_app(
        app,
        host=HOST,
        port=PORT
    )