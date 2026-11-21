"""Local retrieval over the BillWise knowledge base (no services, no extra dependencies).

Two lookups:
  * ``lookup_code(code)``: exact match on a CPT/HCPCS code.
  * ``KnowledgeBase.search(query, k)``: BM25 over each snippet's title, keywords and text,
    used for free-text questions and for line items whose code is missing or unknown.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

KB_PATH = Path(__file__).with_name("knowledge_base.json")

_STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "it", "my", "me", "i", "this",
    "that", "what", "why", "how", "do", "does", "did", "was", "were", "be", "are", "with", "by", "at",
    "as", "from", "per", "can", "you", "your", "am", "there", "their", "they", "so", "if", "about",
}


class Snippet(BaseModel):
    id: str
    kind: str            # "term" | "code"
    title: str
    text: str
    code: str | None = None
    keywords: list[str] = []


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for t in toks:
        if t in _STOP or len(t) == 1:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # crude plural folding: charges -> charge
        out.append(t)
    return out


class KnowledgeBase:
    def __init__(self, snippets: list[Snippet], k1: float = 1.5, b: float = 0.75) -> None:
        self.snippets = snippets
        self.by_id = {s.id: s for s in snippets}
        self.by_code = {s.code: s for s in snippets if s.code}
        self.k1, self.b = k1, b
        # keywords and title are weighted by repeating them
        self.docs = [tokenize(" ".join([s.title] * 2 + s.keywords * 2 + [s.text])) for s in snippets]
        self.tf = [Counter(d) for d in self.docs]
        self.avgdl = sum(len(d) for d in self.docs) / max(len(self.docs), 1)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    @classmethod
    def load(cls, path: Path = KB_PATH) -> "KnowledgeBase":
        data = json.loads(path.read_text(encoding="utf-8"))
        snippets = [Snippet(kind="term", **t) for t in data["terms"]]
        snippets += [Snippet(kind="code", **c) for c in data["codes"]]
        return cls(snippets)

    def lookup_code(self, code: str | None) -> Snippet | None:
        return self.by_code.get(code) if code else None

    def search(self, query: str, k: int = 3, kind: str | None = None) -> list[tuple[Snippet, float]]:
        q = tokenize(query)
        scored = []
        for i, s in enumerate(self.snippets):
            if kind and s.kind != kind:
                continue
            dl = len(self.docs[i])
            score = 0.0
            for t in q:
                f = self.tf[i].get(t, 0)
                if f:
                    score += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if score > 0:
                scored.append((s, score))
        scored.sort(key=lambda x: (-x[1], x[0].id))
        return scored[:k]


@lru_cache(maxsize=1)
def default_kb() -> KnowledgeBase:
    return KnowledgeBase.load()
