#!/usr/bin/env python3
import os, json, time, signal, sys
from pathlib import Path
from openai import OpenAI

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CLEAN_DIR = DATA_DIR / "clean"
SUMMARY_DIR = DATA_DIR / "summarized"

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://brain:8000/v1")
LLM_API_KEY   = os.getenv("LLM_API_KEY", "local")
MODEL_NAME    = os.getenv("LLM_MODEL", "llama-3.1-8b-instruct")
INTERVAL      = int(os.getenv("IDLE_INTERVAL", "60"))

client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

SKIP_CATEGORY_DOCS = os.getenv("SUMMARIZER_SKIP_CATEGORIES", "1")  # default: skip
SUMMARIZER_SKIP_LISTS      = os.getenv("SUMMARIZER_SKIP_LISTS", "1")
MIN_INPUT_CHARS            = int(os.getenv("MIN_INPUT_CHARS", "280"))



def _graceful_exit(signum, frame):
    print("Summarizer shutting down...", flush=True); sys.exit(0)
signal.signal(signal.SIGINT, _graceful_exit)
signal.signal(signal.SIGTERM, _graceful_exit)

def summarize_text(text: str) -> str | None:
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system",
                 "content": "You write concise, factual wiki summaries under 150 words. Include key facts; no fluff."},
                {"role": "user", "content": text[:8000]}  # guardrail: avoid huge prompts
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ERROR] LLM summarization failed: {e}", flush=True)
        return None

def process_once() -> int:
    wrote = 0
    for json_path in sorted(CLEAN_DIR.rglob("*.json")):
        out_path = SUMMARY_DIR / json_path.name
        if out_path.exists():
            continue

        data, text, url, doc_type = {}, "", "", ""
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))

            # get fields FIRST
            url = data.get("url") or ""
            doc_type = (data.get("doc_type") or "").lower()
            text = (data.get("content") or "").strip()   # <-- moved up

            # skip checks AFTER text is set
            if (not url
                or len(text) < MIN_INPUT_CHARS
                or doc_type in ("disambiguation",)
                or (SKIP_CATEGORY_DOCS == "1" and doc_type == "category")
                or (SUMMARIZER_SKIP_LISTS == "1" and doc_type == "list")):
                print(f"[summarizer] skip {doc_type or 'unknown'} {url or data.get('page_id')} "
                      f"chars={len(text)}<min={MIN_INPUT_CHARS}", flush=True)
                continue

            print(f"[summarizer] Summarizing {json_path.relative_to(DATA_DIR)}...", flush=True)
            summary = summarize_text(text)
            if not summary:
                continue
            data["summary"] = summary
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[summarizer] ✅ Saved summary to {out_path}", flush=True)
            wrote += 1
        except Exception as e:
            print(f"[WARN] Failed {json_path}: {e} (type={doc_type or '∅'}, url={url or '∅'}, chars={len(text)})", flush=True)
    return wrote


if __name__ == "__main__":
    print(f"Summarizer service running... (model={MODEL_NAME})", flush=True)
    print(f"Connecting to LLM at {LLM_BASE_URL}", flush=True)
    try:
        models = client.models.list()
        print(f"LLM reachable, {len(models.data)} models available.", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify LLM: {e}", flush=True)

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        n = process_once()
        if n == 0:
            time.sleep(INTERVAL)
