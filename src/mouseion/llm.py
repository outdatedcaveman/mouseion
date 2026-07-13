import json
import logging
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

    if not api_key:
        return None

    try:
        if provider == "openai":
            result = await _call_openai(api_key, text)
        elif provider == "anthropic":
            result = await _call_anthropic(api_key, text)
        elif provider == "google":
            result = await _call_gemini(api_key, text)
        else:
            logger.error(f"Unsupported LLM provider: {provider}")
            _llm_disabled = True
            return None

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
