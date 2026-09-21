"""
Model provider abstraction for triageQ.

Nothing about triageQ's screening or criteria-compiler prompts is
Claude-specific — they're plain text, built entirely in criteria_profiles.py.
What differs between providers is (a) how the API request is shaped and
(b) whether the provider can read a PDF natively ("vision") or only accepts
text. This module isolates both behind one small interface so app.py and
criteria_profiles.py never call a provider SDK directly.

Three providers ship:

  AnthropicProvider       Native PDF vision, via Claude's "document" content
                          block. Requires the `anthropic` package.

  OpenAIProvider          Native PDF vision, via OpenAI's Chat Completions
                          "file" content block (file_data as a base64 data
                          URL) on a vision-capable model. Requires the
                          `openai` package. OpenAI's Responses API supports a
                          richer set of file types, but Chat Completions —
                          used here — is PDF-only, which is all triageQ needs
                          and keeps this provider's shape identical to the
                          other two.

  OpenAICompatProvider    Any OpenAI-compatible /chat/completions endpoint:
                          self-hosted servers (vLLM, llama.cpp, LM Studio,
                          Ollama's OpenAI shim) and hosted aggregators
                          (OpenRouter, Together, Groq, etc). Configured with
                          a base_url instead of a fixed host.

                          Text-only, deliberately. OpenAI's "file" content
                          type is an OpenAI-specific extension of the Chat
                          Completions shape, not something most third-party
                          servers implement, and there is no reliable
                          cross-server standard for PDF input. Rather than
                          silently degrade — or silently fail — on servers
                          that don't support it, this provider never claims
                          PDF vision at all: callers extract text locally
                          (extract_pdf_text, below) and send that instead.
                          Whether that happened is recorded on the repository
                          record (see app.py's pdf_vision_used column), never
                          silently absorbed into "screening_basis": that field
                          already means something else (how much of the paper
                          was available), and conflating it with *how* the
                          available text reached the model would blur two
                          different kinds of "less evidence than a full PDF".

Every provider exposes the same two things:

    provider.supports_pdf_vision   -> bool, checked by the caller BEFORE
                                       deciding whether to pass pdf_bytes
    provider.complete(system_prompt, user_text, pdf_bytes=None,
                       max_tokens=4096) -> str

A provider that doesn't support PDF vision raises if pdf_bytes is passed
anyway — a safety net, not the primary control path. Callers are expected to
check supports_pdf_vision first and extract text themselves when it's False.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass


class ProviderError(RuntimeError):
    """A configuration or request failure, with a message safe to show as-is."""


@dataclass
class ProviderConfig:
    provider: str            # "anthropic" | "openai" | "openai_compatible"
    api_key: str = ""
    model: str = ""
    base_url: str = ""       # openai_compatible only


# ── Shared PDF text extraction, for providers with no native PDF input ────────

def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 300) -> str:
    """Full-document text extraction for text-only providers.

    Not the same job as local_library.py's fingerprinting extraction, which
    deliberately caps output at a small size — that's for identifying which
    paper a file is, not for screening it. This pulls everything a text-only
    model needs to actually apply the criteria.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ProviderError(
            "pypdf is required to screen PDFs with a text-only provider. "
            "Run: pip install pypdf") from exc

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise ProviderError(f"Could not read PDF: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ProviderError("PDF is password protected and could not be read.") from exc

    pages = []
    for page in reader.pages[:max(1, max_pages)]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(pages).strip()
    if not text:
        raise ProviderError(
            "No extractable text in this PDF — likely a scan with no text layer. "
            "A text-only provider cannot screen it. Use Anthropic or OpenAI for "
            "native PDF vision, or supply a text-layer PDF.")
    return text


# ── Providers ───────────────────────────────────────────────────────────────

class ModelProvider:
    supports_pdf_vision: bool = False
    display_name = "Model"

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg

    def complete(self, system_prompt: str, user_text: str,
                 pdf_bytes: bytes | None = None, max_tokens: int = 4096) -> str:
        raise NotImplementedError

    def _reject_pdf(self, pdf_bytes):
        if pdf_bytes:
            raise ProviderError(
                f"{self.display_name} does not support native PDF input. "
                "This is a caller bug — pdf_bytes should never reach a provider "
                "whose supports_pdf_vision is False.")


class AnthropicProvider(ModelProvider):
    supports_pdf_vision = True
    display_name = "Anthropic"

    def complete(self, system_prompt, user_text, pdf_bytes=None, max_tokens=4096):
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError(
                "The 'anthropic' package is not installed. Run: pip install anthropic"
            ) from exc
        if not self.cfg.api_key:
            raise ProviderError("No Anthropic API key configured.")

        content = []
        if pdf_bytes:
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.standard_b64encode(pdf_bytes).decode("utf-8")},
            })
        content.append({"type": "text", "text": user_text})

        client = anthropic.Anthropic(api_key=self.cfg.api_key)
        try:
            resp = client.messages.create(
                model=self.cfg.model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic API error: {exc}") from exc
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


class OpenAIProvider(ModelProvider):
    supports_pdf_vision = True
    display_name = "OpenAI"

    def complete(self, system_prompt, user_text, pdf_bytes=None, max_tokens=4096):
        try:
            import openai
        except ImportError as exc:
            raise ProviderError(
                "The 'openai' package is not installed. Run: pip install openai"
            ) from exc
        if not self.cfg.api_key:
            raise ProviderError("No OpenAI API key configured.")

        content = [{"type": "text", "text": user_text}]
        if pdf_bytes:
            b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
            content.append({
                "type": "file",
                "file": {"filename": "document.pdf",
                         "file_data": f"data:application/pdf;base64,{b64}"},
            })

        client = openai.OpenAI(api_key=self.cfg.api_key)
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                max_completion_tokens=max_tokens,
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": content}],
            )
        except openai.APIError as exc:
            raise ProviderError(f"OpenAI API error: {exc}") from exc
        return resp.choices[0].message.content or ""


class OpenAICompatProvider(ModelProvider):
    supports_pdf_vision = False
    display_name = "OpenAI-compatible"

    def complete(self, system_prompt, user_text, pdf_bytes=None, max_tokens=4096):
        self._reject_pdf(pdf_bytes)
        try:
            import openai
        except ImportError as exc:
            raise ProviderError(
                "The 'openai' package is not installed. Run: pip install openai"
            ) from exc
        if not self.cfg.base_url:
            raise ProviderError(
                "No base URL configured for the OpenAI-compatible endpoint.")

        # Many local servers ignore the key entirely; a placeholder keeps the
        # SDK (which requires a non-empty string) from erroring before the
        # request even goes out.
        client = openai.OpenAI(api_key=self.cfg.api_key or "not-needed",
                               base_url=self.cfg.base_url)
        try:
            resp = client.chat.completions.create(
                model=self.cfg.model,
                max_tokens=max_tokens,
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user_text}],
            )
        except Exception as exc:
            raise ProviderError(f"{self.cfg.base_url}: {exc}") from exc
        return resp.choices[0].message.content or ""


# ── Registry ────────────────────────────────────────────────────────────────

PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
    "openai_compatible": "OpenAI-compatible (local / other)",
}

_REGISTRY = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "openai_compatible": OpenAICompatProvider,
}


def build_provider(cfg: ProviderConfig) -> ModelProvider:
    cls = _REGISTRY.get(cfg.provider)
    if not cls:
        raise ProviderError(f"Unknown provider: {cfg.provider!r}")
    return cls(cfg)


def supports_pdf_vision(provider_name: str) -> bool:
    cls = _REGISTRY.get(provider_name)
    return bool(cls and cls.supports_pdf_vision)
