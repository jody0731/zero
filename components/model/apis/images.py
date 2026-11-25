"""Image generation and editing utility API adapters: OpenAI and Google."""

import base64
from dataclasses import dataclass
from http import HTTPStatus

import httpx2


class ImageError(Exception):
    """An image generation error safe to show without keys or raw headers."""


@dataclass(frozen=True)
class GeneratedImage:
    """One generated or edited image payload."""

    data: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class ImageResponse:
    """Result from an image generation or editing API call."""

    images: tuple[GeneratedImage, ...]
    revised_prompt: str | None = None
    model: str | None = None
    billed: bool | None = None


def _http_error(status):
    if 300 <= status < 400:
        return ImageError(f"HTTP {status}. The image service redirected the request.")
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "unexpected status"
    return ImageError(f"HTTP {status} ({reason}).")


def _ext_for_mime(media_type):
    clean = (media_type or "image/png").split(";", 1)[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(clean, ".png")


class OpenAIImages:
    """OpenAI Image API (`POST /v1/images/generations` & `/v1/images/edits`)."""

    NAME = "openai_images"
    REQUIRES_MODEL = True

    @staticmethod
    def endpoint(base_url):
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/images"
        return f"{base}/v1/images"

    @classmethod
    def generate(
        cls,
        service,
        prompt,
        *,
        size=None,
        quality=None,
        count=1,
        aspect_ratio=None,
        source_images=(),
        seconds=180,
        transport=None,
        check=None,
    ):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageError("Image prompt must not be empty.")
        n = max(1, min(int(count or 1), 4))
        headers = {"Authorization": f"Bearer {service.key}"}
        try:
            with httpx2.Client(
                timeout=seconds, transport=transport, follow_redirects=False,
            ) as client:
                if check is not None:
                    check()
                if source_images:
                    url = f"{service.url.rstrip('/')}/edits"
                    data = {
                        "model": service.model,
                        "prompt": prompt.strip(),
                        "n": str(n),
                    }
                    if size:
                        data["size"] = str(size)
                    if quality:
                        data["quality"] = str(quality)
                    files = [
                        ("image", (fname, raw, mime))
                        for fname, raw, mime in source_images
                    ]
                    reply = client.post(url, data=data, files=files, headers=headers)
                else:
                    url = f"{service.url.rstrip('/')}/generations"
                    body = {
                        "model": service.model,
                        "prompt": prompt.strip(),
                        "n": n,
                    }
                    if size:
                        body["size"] = str(size)
                    if quality:
                        body["quality"] = str(quality)
                    reply = client.post(url, json=body, headers=headers)
                if not reply.is_success:
                    raise _http_error(reply.status_code)
                payload = reply.json()
        except ImageError:
            raise
        except httpx2.TimeoutException:
            raise ImageError(
                f"The image service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise ImageError("The connection to the image service failed.") from None
        except ValueError:
            raise ImageError("The image service returned invalid JSON.") from None

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ImageError("The image response did not include image data.")
        output_format = str(payload.get("output_format") or "png").lower()
        default_mime = (
            f"image/{'jpeg' if output_format == 'jpg' else output_format}"
        )
        found = []
        revised = None
        for idx, item in enumerate(payload["data"], 1):
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json")
            if not isinstance(b64, str) or not b64:
                continue
            if revised is None and isinstance(item.get("revised_prompt"), str):
                revised = item["revised_prompt"]
            raw = base64.b64decode(b64)
            mime = item.get("media_type") or default_mime
            ext = _ext_for_mime(mime)
            found.append(GeneratedImage(raw, mime, f"generated-image-{idx}{ext}"))
        if not found:
            raise ImageError("The image response contained no base64 image data.")
        return ImageResponse(
            images=tuple(found),
            revised_prompt=revised,
            model=service.model,
            billed=True,
        )


class GoogleImages:
    """Google Gemini Image (`gemini-3.1-flash-image`, `responseModalities=IMAGE`)."""

    NAME = "google_images"
    REQUIRES_MODEL = True

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/") + "/models"

    @classmethod
    def generate(
        cls,
        service,
        prompt,
        *,
        size=None,
        quality=None,
        count=1,
        aspect_ratio=None,
        source_images=(),
        seconds=180,
        transport=None,
        check=None,
    ):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageError("Image prompt must not be empty.")
        parts = []
        for _, raw, mime in source_images:
            parts.append({
                "inlineData": {
                    "mimeType": mime or "image/png",
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            })
        parts.append({"text": prompt.strip()})
        gen_config = {"responseModalities": ["IMAGE"]}
        if aspect_ratio:
            gen_config["imageConfig"] = {"aspectRatio": str(aspect_ratio)}
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_config,
        }
        url = f"{service.url.rstrip('/')}/{service.model}:generateContent"
        headers = {"x-goog-api-key": service.key}
        try:
            with httpx2.Client(
                timeout=seconds, transport=transport, follow_redirects=False,
            ) as client:
                if check is not None:
                    check()
                reply = client.post(url, json=body, headers=headers)
                if not reply.is_success:
                    raise _http_error(reply.status_code)
                payload = reply.json()
        except ImageError:
            raise
        except httpx2.TimeoutException:
            raise ImageError(
                f"The image service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise ImageError("The connection to the image service failed.") from None
        except ValueError:
            raise ImageError("The image service returned invalid JSON.") from None

        found = []
        notes = []
        for candidate in (payload.get("candidates") or ()):
            c_parts = ((candidate or {}).get("content") or {}).get("parts") or ()
            for part in c_parts:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str) and part["text"].strip():
                    notes.append(part["text"].strip())
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                    mime = (
                        inline.get("mimeType")
                        or inline.get("mime_type")
                        or "image/png"
                    ).split(";", 1)[0].strip().lower()
                    raw = base64.b64decode(inline["data"])
                    ext = _ext_for_mime(mime)
                    fname = f"generated-image-{len(found) + 1}{ext}"
                    found.append(GeneratedImage(raw, mime, fname))
        if not found:
            raise ImageError("Google Image API returned no image data.")
        return ImageResponse(
            images=tuple(found),
            revised_prompt="\n".join(notes) if notes else None,
            model=service.model,
            billed=True,
        )
