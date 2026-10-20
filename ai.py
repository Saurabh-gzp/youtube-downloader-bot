"""OpenRouter (OpenAI-compatible) helper — optional AI features."""
import logging

import config

log = logging.getLogger("ytdl.ai")

_BASE = "https://openrouter.ai/api/v1"

if config.AI_ENABLED:
    try:
        import httpx
        _HTTPX_OK = True
    except Exception as e:
        log.error("httpx missing, AI disabled: %s", e)
        _HTTPX_OK = False
else:
    _HTTPX_OK = False


async def chat(prompt: str, system: str = "You are a helpful assistant. "
               "Answer briefly and clearly.", max_tokens: int = 300) -> str:
    """One-shot chat completion. Raises on any failure."""
    if not config.AI_ENABLED or not _HTTPX_OK:
        raise RuntimeError("AI disabled")
    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.post(
            f"{_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            json={
                "model": config.AI_MODEL,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        r.raise_for_status()
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()


async def clean_query(text: str) -> str:
    """Turn a casual search line into a clean 'Title - Artist' style query.

    Falls back to the original text on any error — search must never break
    because of the AI layer.
    """
    try:
        out = await chat(
            f"Turn this into a short, clean video search query "
            f"(remove filler words, keep artist/title names, max 8 words, "
            f"reply with the query only):\n\n{text}",
            system="You output only the cleaned search query.",
            max_tokens=40,
        )
        out = out.strip().strip('"').splitlines()[0][:100]
        return out or text
    except Exception:
        return text
