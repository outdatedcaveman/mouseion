import json
import logging
import os
from typing import Optional
import httpx

from .models import Reference, Author, RefType
from .config import get_config

logger = logging.getLogger("mouseion.llm")

_SYSTEM_PROMPT = """
You are an expert academic librarian. Your task is to extract bibliographic metadata from the provided text (which may be a raw citation string, messy OCR text, or an unformatted reference) and return a strict JSON object.

Extract as much of the following as possible:
- title (string)
- doi (string, clean, no url)
- year (integer)
- authors (list of objects with "given" and "family" strings)
- journal (string)
- volume (string)
- issue (string)
- pages (string)
- ref_type (string, one of: "journal-article", "book", "book-chapter", "conference-paper", "preprint", "thesis", "report", "website", "other")

If a field is missing or cannot be confidently determined, omit it from the JSON.
DO NOT include any markdown formatting, only the raw JSON string.
"""

# Circuit breaker: if the LLM endpoint is misconfigured (wrong model → 404,
# bad key → 401, etc.) it must NOT be retried for every ref — that floods the
# log and wastes huge amounts of daemon time. After a few consecutive failures
# we disable LLM parsing for the rest of the process and skip the call entirely.
_llm_consecutive_failures = 0
_llm_disabled = False
_LLM_FAILURE_LIMIT = 5


async def parse_citation_to_ref(text: str) -> Optional[Reference]:
    global _llm_consecutive_failures, _llm_disabled

    if _llm_disabled:
        return None

    cfg = get_config()
    api_key = cfg.llm_api_key
    provider = cfg.llm_provider

    # 'ollama' is LOCAL: no key exists or is needed (Bruno 2026-07-13 — Gemma 4
    # E4B on the idle GPU is the fallback when cloud credits run out).
    if not api_key and provider != "ollama":
        if not await _local_ready():
            return None
        provider = "ollama"          # no cloud key at all → go local

    try:
        try:
            if provider == "openai":
                result = await _call_openai(api_key, text)
            elif provider == "anthropic":
                result = await _call_anthropic(api_key, text)
            elif provider == "google":
                result = await _call_gemini(api_key, text)
            elif provider == "ollama":
                result = await _call_ollama(text)
            else:
                logger.error(f"Unsupported LLM provider: {provider}")
                _llm_disabled = True
                return None
        except Exception as cloud_err:
            # CREDIT/QUOTA/AUTH death → local rescue instead of a dead parser.
            # This is exactly what silently killed enrichment for a week when
            # the Anthropic balance hit zero (404/400 storm, zero output).
            if provider != "ollama" and _is_exhausted(cloud_err) and await _local_ready():
                logger.warning("LLM provider %s exhausted (%s) - falling back to local %s",
                               provider, str(cloud_err)[:60], OLLAMA_MODEL)
                result = await _call_ollama(text)
            else:
                raise

        _llm_consecutive_failures = 0  # success → reset the breaker
        return _json_to_ref(result)
    except Exception as e:
        _llm_consecutive_failures += 1
        if _llm_consecutive_failures >= _LLM_FAILURE_LIMIT:
            _llm_disabled = True
            logger.error(
                "LLM parsing disabled for this session after %d consecutive "
                "failures (last: %s). Fix the provider/model/key and restart.",
                _llm_consecutive_failures, e,
            )
        elif _llm_consecutive_failures == 1:
            logger.error(f"LLM parsing failed: {e}")
        return None

async def _call_openai(api_key: str, text: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text}
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1
            }
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

async def _call_anthropic(api_key: str, text: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                # claude-3-haiku-20240307 (and the whole 3.5 line) is retired →
                # 404. claude-haiku-4-5 is the current cheap/fast model (verified
                # 200 against this account). The circuit breaker above will
                # auto-disable if this ever 404s again after a future refresh.
                "model": "claude-haiku-4-5",
                "system": _SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": text}
                ],
                "max_tokens": 1000,
                "temperature": 0.1
            }
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

async def _call_gemini(api_key: str, text: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.1
                }
            }
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
# granite4:3b (2.1GB) is the largest model that fits the GTX 1650's
# ~2GB usable VRAM; gemma4 (4.3GB+) and phi4-mini/qwen3:4b all OOM.
OLLAMA_MODEL = os.environ.get("MOUSEION_OLLAMA_MODEL", "granite4:3b")


def _is_exhausted(err: Exception) -> bool:
    """Cloud provider is OUT (no credit / quota / auth) — the whole provider is
    unusable, so only a LOCAL model can keep the pipeline alive."""
    s = str(err).lower()
    return any(m in s for m in (
        "credit balance is too low", "insufficient_quota", "insufficient credit",
        "billing", "quota", "401", "403", "429", "invalid api key",
        "authentication", "resource_exhausted", "rate limit",
    ))


async def _local_ready() -> bool:
    """Ollama up AND the fallback model present."""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code != 200:
                return False
            names = [m.get("name", "") for m in (r.json().get("models") or [])]
            return any(n == OLLAMA_MODEL or n.startswith(OLLAMA_MODEL.split(":")[0])
                       for n in names)
    except Exception:
        return False


async def _call_ollama(text: str) -> str:
    """Local Gemma 4 — free, no quota. Uses Ollama's JSON mode so the citation
    parser gets strict JSON back like it does from the cloud providers."""
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "stream": False,
                "format": "json",
                # num_ctx 2048 keeps the model 100% on a 4GB GPU (32768 default
                # spills to CPU and is ~135x slower - measured 2026-07-25).
                "options": {"temperature": 0.1, "num_predict": 1000,
                            "num_ctx": 2048},
            },
        )
        resp.raise_for_status()
        return ((resp.json().get("message") or {}).get("content")) or ""


def _json_to_ref(json_str: str) -> Optional[Reference]:
    try:
        # Models often wrap JSON in ```json ... ``` fences despite instructions.
        # Strip them (and any leading/trailing prose) before parsing.
        s = (json_str or "").strip()
        if s.startswith("```"):
            s = s.split("```", 2)
            s = s[1] if len(s) >= 2 else json_str
            if s.lstrip().lower().startswith("json"):
                s = s.lstrip()[4:]
        first, last = s.find("{"), s.rfind("}")
        if first != -1 and last != -1 and last > first:
            s = s[first:last + 1]
        data = json.loads(s)
        ref = Reference()
        ref.title = data.get("title")
        ref.doi = data.get("doi")
        
        # Handle year - enforce integer
        year_val = data.get("year")
        if year_val:
            try:
                ref.year = int(str(year_val)[:4])
            except ValueError:
                pass
                
        ref.journal = data.get("journal")
        ref.volume = str(data.get("volume")) if data.get("volume") else None
        ref.issue = str(data.get("issue")) if data.get("issue") else None
        ref.pages = str(data.get("pages")) if data.get("pages") else None
        
        # Parse ref type
        rt = data.get("ref_type")
        if rt:
            ref.ref_type = RefType.from_crossref(rt)
            
        # Parse authors
        authors = data.get("authors", [])
        if isinstance(authors, list):
            for a in authors:
                if isinstance(a, dict):
                    fam = a.get("family") or a.get("last") or ""
                    giv = a.get("given") or a.get("first") or ""
                    if fam or giv:
                        ref.authors.append(Author(family=fam, given=giv))
        
        if ref.title or ref.doi:
            ref.sources["llm"] = 0.95
            ref.normalize()
            return ref
    except json.JSONDecodeError:
        logger.error(f"Failed to parse LLM JSON output: {json_str[:100]}")
    except Exception as e:
        logger.error(f"Error converting LLM output to Reference: {e}")
    
    return None
