# =========================================================
# client.py
# =========================================================

import asyncio
import os

import aiohttp
import sounddevice as sd
import av

from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    MediaStreamTrack
)

from dotenv import load_dotenv

# =========================================================
# LOAD ENV
# =========================================================

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

SERVER_IP = os.getenv("SERVER_IP")

PORT = os.getenv("PORT", "8080")

SERVER_URL = (
    f"http://{SERVER_IP}:{PORT}/offer"
)

SAMPLE_RATE = 16000
CHANNELS = 1

# =========================================================
# MICROPHONE TRACK
# =========================================================

class MicrophoneTrack(MediaStreamTrack):

    kind = "audio"

    def __init__(self):

        super().__init__()

        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16',
            blocksize=960
        )

        self.stream.start()

    async def recv(self):

        data, _ = self.stream.read(960)

        frame = av.AudioFrame.from_ndarray(
            data.T,
            format="s16",
            layout="mono"
        )

        frame.sample_rate = SAMPLE_RATE

        return frame

# =========================================================
# SPEAKER
# =========================================================

speaker = sd.OutputStream(
    samplerate=SAMPLE_RATE,
    channels=1,
    dtype='int16',
    blocksize=960
)

speaker.start()

# =========================================================
# MAIN
# =========================================================

async def main():

    pc = RTCPeerConnection()

    mic = MicrophoneTrack()

    pc.addTrack(mic)

    @pc.on("track")
    async def on_track(track):

        print("Receiving translated audio...")

        while True:

            frame = await track.recv()

            pcm = frame.to_ndarray()

            speaker.write(
                pcm.T.copy()
            )

    offer = await pc.createOffer()

    await pc.setLocalDescription(
        offer
    )

    async with aiohttp.ClientSession() as session:

        async with session.post(
            SERVER_URL,
            json={
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type
            }
        ) as response:

            answer = await response.json()

    await pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=answer["sdp"],
            type=answer["type"]
        )
    )

    print("Connected to server.")

    await asyncio.Future()

if __name__ == "__main__":

    asyncio.run(main())