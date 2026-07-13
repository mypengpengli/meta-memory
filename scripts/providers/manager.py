from __future__ import annotations

from .base import CompressionMemoryContribution, MemoryContext, MemoryProvider


class MemoryManager:
    def __init__(self, providers: list[MemoryProvider]) -> None:
        self.providers = providers
        self.errors: list[dict[str, str]] = []

    def _call(self, provider: MemoryProvider, method: str, *args, **kwargs):
        try: return getattr(provider, method)(*args, **kwargs)
        except Exception as exc:
            self.errors.append({"provider": provider.name, "method": method, "error": str(exc)})
            return None

    def initialize(self, **kwargs) -> None:
        for provider in self.providers: self._call(provider, "initialize", **kwargs)

    def static_prompt_block(self) -> str:
        return "\n\n".join(block for provider in self.providers if (block := self._call(provider, "static_prompt_block"))).strip()

    def prefetch(self, query: str, *, session_id: str, token_budget: int) -> MemoryContext:
        contexts = [item for provider in self.providers if (item := self._call(provider, "prefetch", query, session_id=session_id, token_budget=token_budget))]
        return MemoryContext(text="\n".join(item.text for item in contexts if item.text), claims=[claim for item in contexts for claim in item.claims], session_evidence=[evidence for item in contexts for evidence in item.session_evidence], snapshot_hash="|".join(item.snapshot_hash for item in contexts if item.snapshot_hash))

    def sync_turn(self, *args, **kwargs) -> None:
        for provider in self.providers: self._call(provider, "sync_turn", *args, **kwargs)

    def on_session_end(self, messages: list[dict]) -> None:
        for provider in self.providers: self._call(provider, "on_session_end", messages)

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        for provider in self.providers: self._call(provider, "on_session_switch", new_session_id, **kwargs)

    def on_pre_compress(self, messages: list[dict]) -> CompressionMemoryContribution:
        results = [item for provider in self.providers if (item := self._call(provider, "on_pre_compress", messages))]
        return CompressionMemoryContribution([value for item in results for value in item.flushed_event_ids], [value for item in results for value in item.memory_units_created], [value for item in results for value in item.compression_hints])

    def shutdown(self) -> None:
        for provider in self.providers: self._call(provider, "shutdown")
