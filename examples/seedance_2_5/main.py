"""Generate one Seedance 2.5 video through the Higgsfield API with the official SDK.

This makes a billable generation request. Run it from the repository root:

    uv run python -m examples.seedance_2_5.main

Credentials come from ``HF_KEY="<key id>:<key secret>"`` in ``.env.local`` at the
repository root (git-ignored). They are loaded into the environment at runtime
and never printed. Progress goes to stderr; on success the video URL is the only
line on stdout. Exit codes: 0 video ready, 1 no video, 2 credentials missing.

This is a standalone example. Hatch's own pipeline uses its adapter in
``integrations/higgsfield`` with the budget governor in front of every call.
"""

import sys
from pathlib import Path
from typing import Any

import higgsfield_client
import httpx
from dotenv import load_dotenv

MODEL = "bytedance/seedance-2.5/text-to-video"
ARGUMENTS: dict[str, Any] = {
    "prompt": "A cinematic scene at sunset",
    "duration": 5,
    "resolution": "720p",
    "aspect_ratio": "16:9",
}
ENV_FILE = Path(__file__).resolve().parents[2] / ".env.local"


class GenerationNotCompleted(Exception):
    """The request ended without a video: failed, moderated (nsfw) or canceled."""


def video_url(result: dict[str, Any]) -> str:
    """The generated video's URL, or an error if the request produced none.

    The SDK's ``subscribe`` returns the final payload for every terminal status
    instead of raising, so success has to be checked here.
    """
    status = result.get("status")
    request_id = result.get("request_id")
    if status != "completed":
        detail = result.get("error") or "no error message"
        raise GenerationNotCompleted(f"request {request_id} ended with status {status!r}: {detail}")
    url = (result.get("video") or {}).get("url")
    if not isinstance(url, str) or not url:
        raise GenerationNotCompleted(f"request {request_id} completed but returned no video URL")
    return url


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main() -> int:
    load_dotenv(ENV_FILE)
    last_status: list[str] = []

    def on_queue_update(status: higgsfield_client.Status) -> None:
        name = type(status).__name__
        if last_status[-1:] != [name]:
            last_status.append(name)
            _log(f"status: {name}")

    try:
        result = higgsfield_client.subscribe(
            MODEL,
            arguments=ARGUMENTS,
            on_enqueue=lambda request_id: _log(f"submitted request {request_id}"),
            on_queue_update=on_queue_update,
        )
        url = video_url(result)
    except higgsfield_client.CredentialsMissedError:
        _log(f"HF_KEY is not set. Add HF_KEY=<key id>:<key secret> to {ENV_FILE}.")
        return 2
    except higgsfield_client.HiggsfieldClientError as error:
        _log(f"Higgsfield rejected the request: {error}")
        return 1
    except httpx.HTTPError as error:
        _log(f"Network error talking to Higgsfield: {type(error).__name__}: {error}")
        return 1
    except GenerationNotCompleted as error:
        _log(f"No video was generated: {error}")
        return 1
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
