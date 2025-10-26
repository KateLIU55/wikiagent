#!/usr/bin/env python3
import os, json, time, signal, sys
from pathlib import Path
from openai import OpenAI

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CLEAN_DIR = DATA_DIR / "clean"
SUMMARY_DIR = DATA_DIR / "summarized"

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:1234/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "local")
MODEL_NAME = os.getenv("LLM_MODEL", "meta-llama-3.1-8b-instruct")
INTERVAL = int(os.getenv("IDLE_INTERVAL", "60"))  # seconds between scans

client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

def _graceful_exit(signum, frame):
    print("Summarizer shutting down...", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_exit)
signal.signal(signal.SIGTERM, _graceful_exit)

def summarize_text(text):
    """Send text to the local LLM and return a short summary."""
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a summarizer that writes concise, factual summaries "
                        "in under 150 words suitable for a wiki entry."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ERROR] LLM summarization failed: {e}", flush=True)
        return None

def process_once():
    """Scan /data/clean for new .json files and summarize them."""
    wrote = 0
    for json_path in sorted(CLEAN_DIR.glob("*.json")):
        out_path = SUMMARY_DIR / json_path.name
        if out_path.exists():
            continue  # already summarized

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            content = data.get("content", "")
            if not content.strip():
                continue

            print(f"[summarizer] Summarizing {json_path.name}...", flush=True)
            summary = summarize_text(content)
            if summary:
                data["summary"] = summary
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                print(f"[summarizer] ✅ Saved summary to {out_path}", flush=True)
                wrote += 1
        except Exception as e:
            print(f"[WARN] Failed {json_path}: {e}", flush=True)
    return wrote

if __name__ == "__main__":
    print(f"Summarizer service running... (model={MODEL_NAME})", flush=True)
    print(f"Connecting to LLM at {LLM_BASE_URL}", flush=True)

    # Quick test connection
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
