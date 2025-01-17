#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os
import sys
import webbrowser

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from loguru import logger
from pipecat_flows import FlowArgs, FlowConfig, FlowManager, FlowResult
from runner import configure

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.google import GoogleLLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

# Fixed port required for Spotify Redirect URI
PORT = 8585
REDIRECT_URI = f"http://localhost:{PORT}/callback"
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SCOPES = "playlist-modify-public playlist-modify-private user-read-playback-state user-modify-playback-state user-read-private"

user_access_token = None
playlist_uri = None

playlist_type = None
song_count = None

tts = None

async def get_spotify_token():
    async with aiohttp.ClientSession() as session:
        auth = aiohttp.BasicAuth(os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET"))
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "client_credentials"}
        async with session.post(
            SPOTIFY_TOKEN_URL, headers=headers, data=data, auth=auth
        ) as response:
            token_data = await response.json()
            return token_data["access_token"]


async def get_user_access_token():
    global user_access_token

    if user_access_token:
        return user_access_token

    async def handle_callback(request):
        global user_access_token
        code = request.query.get("code")

        async with aiohttp.ClientSession() as session:
            auth = aiohttp.BasicAuth(
                os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
            )
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            }
            async with session.post(
                SPOTIFY_TOKEN_URL, headers=headers, data=data, auth=auth
            ) as response:
                token_data = await response.json()
                if "access_token" in token_data:
                    user_access_token = token_data["access_token"]
                    logger.info("Authentication successful.")
                    return web.Response(
                        text="Authentication successful. You can close this window.", status=200
                    )
                else:
                    logger.error(f"Authentication failed: {token_data}")
                    return web.Response(text="Authentication failed. Please try again.", status=400)

    app = web.Application()
    app.router.add_get("/callback", handle_callback)
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "localhost", PORT)
    await site.start()

    try:
        auth_url = (
            f"{SPOTIFY_AUTH_URL}?response_type=code&client_id={os.getenv('SPOTIFY_CLIENT_ID')}"
            f"&scope={SPOTIFY_SCOPES}&redirect_uri=http://localhost:{PORT}/callback"
        )
        webbrowser.open(auth_url)

        # Wait at least 8 seconds for the LLM to update context
        await asyncio.sleep(8)
        while not user_access_token:
            await asyncio.sleep(1)
    finally:
        await runner.cleanup()

    return user_access_token


async def search_song(query, token):
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            f"https://api.spotify.com/v1/search?q={query}&type=track&limit=1", headers=headers
        ) as response:
            data = await response.json()
            items = data.get("tracks", {}).get("items", [])
            if items:
                return items[0]["uri"]
            return None


async def check_premium_subscription(token):
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get("https://api.spotify.com/v1/me", headers=headers) as response:
            data = await response.json()
            print(data)
            return data.get("product") == "premium"


async def create_playlist(args: FlowArgs):
    global playlist_uri
    global tts

    title = args["title"]
    songs_str = args["songs"]
    songs = [song.strip() for song in songs_str.split(";")]

    if len(songs) > 50:
        await tts.say("I am creating the playlist for you now. This might take a little while.")
    elif len(songs) > 20:
        await tts.say("Hang on, while I'm creating the playlist for you.")
    else:
        await tts.say("Alright, one moment please.")

    token = await get_user_access_token()

    try:
        # Create the playlist
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            user_id = await get_user_id(token)
            async with session.post(
                f"https://api.spotify.com/v1/users/{user_id}/playlists",
                headers=headers,
                json={"name": title, "public": False},
            ) as response:
                if response.status != 201:
                    raise Exception(f"Failed to create playlist. Status code: {response.status}")
                data = await response.json()
                playlist_uri = data["uri"]
                playlist_id = data["id"]

        # Search for each song and get the URI
        uris = []
        for song in songs:
            uri = await search_song(song, token)
            if uri:
                uris.append(uri)

        # Add songs to the playlist
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            async with session.post(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                headers=headers,
                json={"uris": uris},
            ) as response:
                if response.status != 201:
                    raise Exception(
                        f"Failed to add songs to the playlist. Status code: {response.status}"
                    )
                logger.info("Songs added to the playlist.")

        # Check if the user has a premium subscription
        if await check_premium_subscription(token):
            return {"success": True, "premium": True}
        else:
            return {"success": True, "premium": False}
    except Exception as e:
        logger.error(f"Error in create_playlist: {e}")
        return {"success": False, "error": str(e)}


async def get_user_id(token):
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get("https://api.spotify.com/v1/me", headers=headers) as response:
            data = await response.json()
            return data["id"]


async def start_playlist(args: FlowArgs):
    global playlist_uri
    token = await get_user_access_token()

    if not playlist_uri:
        logger.error("No playlist URI found.")
        return {"success": False, "error": "No playlist URI found."}

    webbrowser.open(playlist_uri)

    try:
        # Get the user's devices
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {token}"}
            async with session.get(
                "https://api.spotify.com/v1/me/player/devices", headers=headers
            ) as response:
                data = await response.json()
                devices = data.get("devices", [])
                if not devices:
                    logger.error("No devices found.")
                    return {"success": False, "error": "No devices found."}
                device_id = devices[0]["id"]

        # Start playback on the first available device
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            data = {"context_uri": playlist_uri, "device_id": device_id}
            print(data)
            async with session.put(
                "https://api.spotify.com/v1/me/player/play", headers=headers, json=data
            ) as response:
                if response.status != 204:
                    raise Exception(f"Failed to start playback. Status code: {response.status} {await response.text()}")
                logger.info(f"Playlist is now playing on device {device_id}.")
                return {"success": True}
    except Exception as e:
        logger.error(f"Error in start_playlist: {e}")
        return {"success": False, "error": str(e)}


async def authenticate_user():
    try:
        user_access_token = await asyncio.wait_for(get_user_access_token(), timeout=20)
        return {"success": True, "access_token": user_access_token}
    except asyncio.TimeoutError:
        logger.error("Authentication timed out.")
        return {"success": False, "error": "Authentication timed out."}


flow_config: FlowConfig = {
    "initial_node": "greeting",
    "nodes": {
        "greeting": {
            "role_messages": [
                {
                    "role": "system",
                    "content": """
                    You are Stan, a friendly Spotify playlist curator.
                    Your responses will be converted to audio, so avoid special characters.
                    Always use the available functions to progress the conversation naturally.
                    """,
                }
            ],
            "task_messages": [
                {
                    "role": "system",
                    "content": """
                    Start by introducing yourself briefly and asking the user to authenticate with Spotify first.
                    Call authenticate_user to trigger the Spotify authentication flow.
                    """,
                }
            ],
            "functions": [
                {
                    "function_declarations": [
                        {
                            "name": "authenticate_user",
                            "handler": authenticate_user,
                            "description": "Attempts to authenticate the user with Spotify.",
                            "parameters": None,
                            "transition_to": "create_playlist",
                        },
                        {
                            "name": "end_conversation",
                            "description": "End the conversation",
                            "parameters": None,
                            "transition_to": "end",
                        },
                    ]
                }
            ],
        },
        "create_playlist": {
            "task_messages": [
                {
                    "role": "system",
                    "content": """
                    Ask what kind of playlist the user likes to create and how many songs it should contain (max is 100).
                    Playlists can also be created through creative ways, e.g. to have the song titles read like a poem or something similar.
                    Once the user has chosen the type and amount of songs, generate a list of suitable songs in the format '<artist> <song title>' and suggest a playlist title.
                    NEVER read out the entire list, only mention the top 3 artists on the list.
                    Confirm with the user before calling create_playlist.
                    """,
                }
            ],
            "functions": [
                {
                    "function_declarations": [
                        {
                            "name": "create_playlist",
                            "handler": create_playlist,
                            "description": "Create playlist with the given title and songs. After creating the playlist, silently check if the user has a premium subscription. If yes, ask if they'd like to play it, but don't mention their subscription.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "title": {
                                        "type": "string",
                                        "description": "Title of the playlist",
                                    },
                                    "songs": {
                                        "type": "string",
                                        "description": "List of songs separated by semicolons",
                                    },
                                },
                                "required": ["title", "songs"],
                            },
                            "transition_to": "ask_to_play"
                        },
                        {
                            "name": "end_conversation",
                            "description": "End the conversation",
                            "parameters": None,
                            "transition_to": "end",
                        },
                    ]
                }
            ],
        },
        "ask_to_play": {
            "task_messages": [
                {
                    "role": "system",
                    "content": """
                    The playlist is now created.
                    If the user has a premium subscription, ask if they'd like to play the playlist in Spotify and call start_playlist after confirming.
                    Otherwise end the conversation via end_conversation.
                    """,
                }
            ],
            "functions": [
                {
                    "function_declarations": [
                        {
                            "name": "start_playlist",
                            "handler": start_playlist,
                            "description": "Start playing the playlist. If successful briefly say goodbye.",
                            "parameters": None,
                            "transition_to": "end",
                        },
                        {
                            "name": "end_conversation",
                            "description": "End the conversation",
                            "parameters": None,
                            "transition_to": "end",
                        },
                    ]
                }
            ],
        },
        "end": {
            "task_messages": [
                {
                    "role": "system",
                    "content": "Thank the user warmly and mention they can return anytime to create playlists together.",
                }
            ],
            "functions": [],
            "post_actions": [{"type": "end_conversation"}],
        },
    },
}


def open_spotify_app():
    spotify_uri = "spotify:app:home"
    webbrowser.open(spotify_uri)


async def main():
    open_spotify_app()
    async with aiohttp.ClientSession() as session:
        global tts
        (room_url, token) = await configure(session)

        transport = DailyTransport(
            room_url,
            token,
            "Spotify Playlist Bot",
            DailyParams(
                audio_out_enabled=True,
                vad_enabled=True,
                vad_analyzer=SileroVADAnalyzer(),
                vad_audio_passthrough=True,
            ),
        )

        llm = GoogleLLMService(api_key=os.getenv("GOOGLE_API_KEY"), model="gemini-2.0-flash-exp")

        stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id=os.getenv("CARTESIA_VOICE_ID"),
            text_filter=MarkdownTextFilter(),
        )

        context = OpenAILLMContext()
        context_aggregator = llm.create_context_aggregator(context)

        pipeline = Pipeline(
            [
                transport.input(),  # Transport user input
                stt,  # STT
                context_aggregator.user(),  # User responses
                llm,  # LLM
                tts,  # TTS
                transport.output(),  # Transport bot output
                context_aggregator.assistant(),  # Assistant spoken responses
            ]
        )

        task = PipelineTask(
            pipeline,
            PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )

        flow_manager = FlowManager(task=task, llm=llm, tts=tts, flow_config=flow_config)

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            await flow_manager.initialize()
            await task.queue_frames([context_aggregator.user().get_context_frame()])

        runner = PipelineRunner()

        await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
