import os, json, time, asyncio, logging, pickle, pathlib
from typing import List, Optional, Dict, Any

import uvicorn
import httpx
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Optional FAISS + embeddings; app works without them.
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2")
USE_FAISS = True
try:
    import faiss  # type: ignore
    from sentence_transformers import SentenceTransformer
except Exception:
    USE_FAISS = False

# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------
OPENROUTER_API_KEY   = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL  = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DEFAULT_MODELS       = os.getenv("DEFAULT_MODELS", "openai/gpt-4o,meta-llama/llama-3-70b-instruct,mistralai/mistral-7b-instruct").split(",")
DEFAULT_JUDGE_MODEL  = os.getenv("JUDGE_MODEL", "openai/gpt-4o-mini")
DEFAULT_TOPK         = int(os.getenv("DEFAULT_TOPK", "6"))
DEFAULT_MAX_CTX_TOK  = int(os.getenv("DEFAULT_MAX_CTX_TOK", "2000"))
CORS_ORIGINS         = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]

# Optional fixed FAISS path on the server (can be overridden per request)
FAISS_INDEX_DIR      = os.getenv("FAISS_INDEX_DIR", "")  # e.g. /data/faiss_index

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = FastAPI(title="PHAGES Pipeline API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if CORS_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("phages-api")
logging.basicConfig(level=logging.INFO)

# -----------------------------------------------------------------------------
# Lazy loaders for FAISS + embedder
# -----------------------------------------------------------------------------
_embedder = None
_faiss_index = None
_faiss_texts = None

def _load_embedder():
    global _embedder
    if _embedder is None and USE_FAISS:
        _embedder = SentenceTransformer(EMBED_MODEL_NAME)
    return _embedder

def _try_load_faiss(index_dir: str) -> bool:
    """
    Load FAISS index + texts.pkl from a directory.
    Returns True if loaded, False if anything fails.
    """
    global _faiss_index, _faiss_texts

    try:
        path = pathlib.Path(index_dir)
        idx = path / "index.faiss"
        pkl = path / "texts.pkl"
        if not idx.exists() or not pkl.exists():
            return False
        _faiss_index = faiss.read_index(str(idx))
        with open(pkl, "rb") as f:
            _faiss_texts = pickle.load(f)
        return True
    except Exception as e:
        logger.warning(f"FAISS load failed: {e}")
        _faiss_index, _faiss_texts = None, None
        return False

def _faiss_search(query: str, top_k: int) -> List[str]:
    if not USE_FAISS or _faiss_index is None or _faiss_texts is None:
        return []
    emb = _load_embedder().encode([query])
    D, I = _faiss_index.search(emb, top_k)
    out = []
    for idx in I[0]:
        if 0 <= idx < len(_faiss_texts):
            out.append(_faiss_texts[idx])
    return out

# -----------------------------------------------------------------------------
# OpenRouter call
# -----------------------------------------------------------------------------
async def call_openrouter_model(model: str, messages: List[Dict[str, Any]], timeout_s: int = 60) -> str:
    """
    Calls OpenRouter chat completions for a single model.
    Returns the message content or 'No response'.
    """
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # Optional but nice for OpenRouter analytics:
        "HTTP-Referer": os.getenv("OR_REFERRER", "https://phages.ai"),
        "X-Title": "PHAGES Pipeline",
    }
    payload = {
        "model": model,
        "messages": messages
    }
    try:
        async with httpx.AsyncClient(base_url=OPENROUTER_BASE_URL, timeout=timeout_s) as client:
            r = await client.post("/chat/completions", json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning(f"{model} HTTP {r.status_code}: {r.text[:200]}")
                return "No response"
            data = r.json()
            return (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip() or "No response"
    except Exception as e:
        logger.warning(f"{model} error: {e}")
        return "No response"

# -----------------------------------------------------------------------------
# Consensus + PHAGES prompts
# -----------------------------------------------------------------------------
def build_system_prompt() -> str:
    return (
        "You are Forest Rohwer, creator of the P.H.A.G.E.S. framework. "
        "You analyze ecosystems using Predation, History, Assembly, Governors, Expansion, Selection, and the Goldilocks Line. "
        "Be precise, scientific, concise, and concrete."
    )

def build_competition_prompt(ecosystem: str, why: str, user_prompt: str, retrieved: List[str]) -> str:
    ctx = ""
    if retrieved:
        ctx = "\n\n[Retrieved context from FAISS (may contain partials; use critically)]\n" + "\n---\n".join(retrieved[:5])
    return (
        f"{user_prompt}\n\n"
        "Analyze the ecosystem above using the PHAGES framework.\n"
        f"[Ecosystem]: {ecosystem}\n\n"
        f"[Why it matters]: {why}\n"
        "Be specific to this case; avoid generic filler."
        f"{ctx}\n"
    )

def build_judge_prompt(model_outputs: Dict[str, str]) -> str:
    blocks = []
    for m, txt in model_outputs.items():
        blocks.append(f"=== {m} ===\n{txt}\n")
    joined = "\n".join(blocks)
    return (
        "Two or more models have produced PHAGES analyses of the same ecosystem.\n"
        "Your task: produce an expert synthesis that is concise, precise, and scientifically grounded.\n"
        "Strict requirements:\n"
        "- Be accurate to the PHAGES framework.\n"
        "- Remove filler and repetition.\n"
        "- Use active voice and concrete statements.\n"
        "- Integrate the Goldilocks Line naturally.\n"
        "- Keep total length well under 600 words.\n\n"
        f"{joined}\n\n"
        "Return only the final edited analysis. No preamble."
    )

def build_synthesis_prompts(final_analysis: str) -> Dict[str, str]:
    return {
        "synthesis": (
            "Condense the following PHAGES analysis into a tight synthesis that a scientist could read in ~120 words.\n\n"
            f"{final_analysis}"
        ),
        "improve": (
            "Given the following PHAGES analysis, list 3–5 concrete ways to improve its rigor or clarity. "
            "Be specific (e.g., missing data, weak assumptions, better contrasts).\n\n"
            f"{final_analysis}"
        ),
        "extend": (
            "Propose 2–3 testable hypotheses or next experiments that logically extend from the following PHAGES analysis. "
            "Keep each item very concise, scientifically specific, and actionable.\n\n"
            f"{final_analysis}"
        ),
        "summary": (
            "Write a 5–7 sentence plain-language summary of the following PHAGES analysis for an informed layperson (no jargon).\n\n"
            f"{final_analysis}"
        ),
        "eli5": (
            "Explain the core idea of the following PHAGES analysis for a curious 12-year-old in 4–6 short sentences.\n\n"
            f"{final_analysis}"
        ),
        "critique": (
            "Offer a brief critique of the following PHAGES analysis: key assumptions, possible confounders, and limits to generalization. "
            "Keep it to 5 bullets.\n\n"
            f"{final_analysis}"
        )
    }

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True, "ts": time.time()}

@app.post("/analyze")
async def analyze(
    mode: str = Form("ecosystem"),            # "ecosystem" | "article" (article optional)
    ecosystem: str = Form(""),
    why: str = Form(""),
    user_prompt: str = Form(""),
    models: str = Form(""),                   # JSON list as string
    top_k: int = Form(DEFAULT_TOPK),
    max_context_tokens: int = Form(DEFAULT_MAX_CTX_TOK),
    faiss_path: str = Form(""),
    pdf: Optional[UploadFile] = File(None)    # reserved for future Article mode
):
    """
    Main pipeline:
    - optional FAISS retrieval from provided (or default) index dir
    - parallel model calls via OpenRouter
    - judge/synthesize on DEFAULT_JUDGE_MODEL
    - build PHAGES outputs
    """
    if not OPENROUTER_API_KEY:
        return {"ok": False, "error": "Missing OPENROUTER_API_KEY"}

    # Optional FAISS load
    retrieved = []
    effective_index = faiss_path or FAISS_INDEX_DIR
    if USE_FAISS and effective_index:
        if _faiss_index is None or _faiss_texts is None:
            _try_load_faiss(effective_index)
        if _faiss_index is not None:
            # Use ecosystem+why as query for retrieval
            query = f"{ecosystem} {why}".strip() or ecosystem or why
            if query:
                retrieved = _faiss_search(query, top_k)

    # Models to use
    try:
        mdl_list = json.loads(models) if models else DEFAULT_MODELS
        if not isinstance(mdl_list, list):
            mdl_list = DEFAULT_MODELS
    except Exception:
        mdl_list = DEFAULT_MODELS
    mdl_list = [m.strip() for m in mdl_list if m.strip()][:5]
    if len(mdl_list) < 2:
        mdl_list = DEFAULT_MODELS[:2]

    system_prompt = build_system_prompt()
    comp_prompt   = build_competition_prompt(ecosystem, why, user_prompt, retrieved)

    # Fan-out calls
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": comp_prompt},
    ]

    async def run_models():
        tasks = [call_openrouter_model(m, msgs) for m in mdl_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = {}
        for m, r in zip(mdl_list, results):
            if isinstance(r, Exception):
                out[m] = "No response"
            else:
                out[m] = r or "No response"
        return out

    model_outputs = await run_models()

    # Build consensus (judge/synthesis)
    judge_prompt = build_judge_prompt(model_outputs)
    judge_msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": judge_prompt},
    ]
    final_analysis = await call_openrouter_model(DEFAULT_JUDGE_MODEL, judge_msgs)

    # Derivative pieces (synthesis, improve, extend, summary/eli5/critique)
    prompts = build_synthesis_prompts(final_analysis)
    async def _ask(p):  # helper
        msg = [{"role":"system","content":system_prompt},{"role":"user","content":p}]
        return await call_openrouter_model(DEFAULT_JUDGE_MODEL, msg)

    synthesis, improve, extend, summary, eli5, critique = await asyncio.gather(
        _ask(prompts["synthesis"]),
        _ask(prompts["improve"]),
        _ask(prompts["extend"]),
        _ask(prompts["summary"]),
        _ask(prompts["eli5"]),
        _ask(prompts["critique"]),
    )

    resp = {
        "ok": True,
        "per_model": model_outputs,  # optional for debugging/teacher view
        "consensus": {
            "final_summary": summary,
            "final_eli5":   eli5,
            "final_critique": critique
        },
        "phages": {
            "analysis":  final_analysis,
            "synthesis": synthesis,
            "improve":   improve,
            "extend":    extend
        }
    }
    return resp

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
