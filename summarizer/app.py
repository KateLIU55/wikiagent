#!/usr/bin/env python3
import os, json, time, signal, sys, re
from pathlib import Path
from typing import Optional, Dict, Tuple
from openai import OpenAI
from typing import Optional


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CLEAN_DIR = DATA_DIR / "clean"
SUMMARY_DIR = DATA_DIR / "summarized"

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://brain:8000/v1")
LLM_API_KEY   = os.getenv("LLM_API_KEY", "local")
MODEL_NAME    = os.getenv("LLM_MODEL", "llama-3.1-8b-instruct")
INTERVAL      = int(os.getenv("IDLE_INTERVAL", "60"))

client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

SKIP_CATEGORY_DOCS = os.getenv("SUMMARIZER_SKIP_CATEGORIES", "1")
SUMMARIZER_SKIP_LISTS      = os.getenv("SUMMARIZER_SKIP_LISTS", "1")
MIN_INPUT_CHARS            = int(os.getenv("MIN_INPUT_CHARS", "280"))
MAX_LLM_CHARS             = int(os.getenv("MAX_LLM_CHARS", "3500")) 

def strip_chinese_notes(text: Optional[str]) -> Optional[str]:
    """
    Remove note lines like '注：...' from Chinese summaries.
    Returns cleaned text or None if it becomes empty.
    """
    if not text:
        return text

    keep_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        # Drop any line that starts with '注：' or '注意：'
        if stripped.startswith("注：") or stripped.startswith("注意："):
            continue
        keep_lines.append(line)

    cleaned = "\n".join(keep_lines).strip()
    return cleaned or None


def _graceful_exit(signum, frame):
    print("Summarizer shutting down...", flush=True); sys.exit(0)
signal.signal(signal.SIGINT, _graceful_exit)
signal.signal(signal.SIGTERM, _graceful_exit)

def chat_once(system_prompt: str, user_text: str) -> Optional[str]:
    # Hard-cap the amount of text we send to the LLM to avoid context errors
    text = (user_text or "")
    if len(text) > MAX_LLM_CHARS:
        print(
            f"[summarizer] truncating input from {len(text)} to {MAX_LLM_CHARS} chars",
            flush=True,
        )
        text = text[:MAX_LLM_CHARS]

    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[ERROR] LLM call failed: {e}", flush=True)
        return None

def summarize_en(source_text: str) -> Optional[str]:
    return chat_once(
        "Write a concise, factual wiki-style summary (3–6 sentences, <150 words). "
        "Plain text only. Include key facts present in the input; do not invent details.",
        source_text,
    )

def summarize_zh(source_text: str, use_trad: bool, main_title: Optional[str]) -> Optional[str]:
    # Summarize directly in Chinese (NOT translate). Stop bullets/markdown.
    script = "Traditional Chinese" if use_trad else "Simplified Chinese"
    sys_prompt = (
        f"根據以下中文資料，用{script}撰寫百科式摘要，3–6句，<180字。"
        "只使用自然段落，不能使用項目符號、標題或任何Markdown標記。"
        "忠實於輸入內容，不要新增事實。"
    )
    if main_title:
        # Encourage using the canonical Chinese title if provided
        sys_prompt += f" 本條目的中文標題為「{main_title}」，提及主體時請使用此名稱。"
    return chat_once(sys_prompt, source_text)

def translate_zh(en_summary: str, use_trad: bool, main_title: Optional[str]) -> Optional[str]:
    if use_trad:
        sys_prompt = (
            "Translate the English wiki summary into Traditional Chinese. "
            "保持事實一致，不要新增內容；輸出自然段落，不能使用任何標記。"
        )
        if main_title:
            sys_prompt += f" 主體的中文名稱是「{main_title}」，請在譯文中使用。"
    else:
        sys_prompt = (
            "Translate the English wiki summary into Simplified Chinese. "
            "保持事实一致，不要新增内容；输出自然段落，不能使用任何标记。"
        )
        if main_title:
            sys_prompt += f" 主体的中文名称是“{main_title}”，请在译文中使用。"
    return chat_once(sys_prompt, en_summary)

def process_once() -> int:
    wrote = 0
    for json_path in sorted(CLEAN_DIR.rglob("*.json")):
        out_path = SUMMARY_DIR / json_path.name
        if out_path.exists():
            continue

        data = {}
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            url = data.get("url") or ""
            doc_type = (data.get("doc_type") or "").lower()

            categories = data.get("categories") or []
            derived_tags = set()

            # mapping:
            CATEGORY_TAG_MAP = {
                "Tourist attractions in Nanjing": "景点",
                "History of Nanjing": "历史",
                "Cuisine of Nanjing": "美食",
                "Parks in Nanjing": "公园",
                "Museums in Nanjing": "博物馆",
                "Universities and colleges in Nanjing": "高校",
                "Sports in Nanjing": "体育",
                "Transportation in Nanjing": "交通",
                "Economy of Nanjing": "经济",
                "Culture in Nanjing": "文化",
                "Geography of Nanjing": "地理",
                "Historic sites in Nanjing": "历史遗迹",
                "Mass media in Nanjing": "媒体",
                "Religion in Nanjing": "宗教",
                "Government of Nanjing": "政府",
                "Nanjing": "南京",
                "Buildings and structures in Nanjing": "建筑",
                "Events in Nanjing": "事件",
                "Arts in Nanjing": "艺术",
                "Science and technology in Nanjing": "科技",
                "Notable people from Nanjing": "名人",
                "Companies based in Nanjing": "公司",
                "Hospitals in Nanjing": "医院",
                "Bridges in Nanjing": "桥梁",
                "Streets in Nanjing": "街道",
                "Rivers of Nanjing": "河流",
                "Lakes of Nanjing": "湖泊",
                "Mountains of Nanjing": "山脉",
                "Festivals in Nanjing": "节日",
                "Tourism in Nanjing": "旅游",
                
                # add more as you go
            }

            for cat in categories:
                # English category -> Chinese tag via map
                tag = CATEGORY_TAG_MAP.get(cat)
                if tag:
                    derived_tags.add(tag)

            # Always keep a generic 'summary' tag too
            if not derived_tags:
                derived_tags.add("summary")
            else:
                derived_tags.add("summary")

            # Store tags into summarized JSON
            data["tags"] = sorted(derived_tags)



            if (
                not url
                or doc_type in ("disambiguation",)
                or (SKIP_CATEGORY_DOCS == "1" and doc_type == "category")
                or (SUMMARIZER_SKIP_LISTS == "1" and doc_type == "list")
            ):
                print(f"[summarizer] skip {doc_type or 'unknown'} {url}", flush=True)
                continue

            # Source texts
            en_text       = (data.get("content") or "").strip()
            zh_hans_text  = (data.get("content_zh_hans") or "").strip()
            zh_hant_text  = (data.get("content_zh_hant") or "").strip()
            zh_title_hans = (data.get("zh_title_hans") or "").strip() or None

            if len(en_text) < MIN_INPUT_CHARS and not (len(zh_hans_text) >= MIN_INPUT_CHARS or len(zh_hant_text) >= MIN_INPUT_CHARS):
                print(f"[summarizer] too-short content {url}", flush=True)
                continue

            print(f"[summarizer] Summarizing {json_path.relative_to(DATA_DIR)} (zh_url={bool(data.get('zh_url'))})", flush=True)

            # English summary (always from English content)
            en = summarize_en(en_text) if len(en_text) >= 80 else None

            # Do we have any Chinese page/link?
            have_zh_page = bool(
                (data.get("zh_url") or "").strip() or
                (data.get("content_zh_hans") or "").strip() or
                (data.get("content_zh_hant") or "").strip()
            )
            hans = None  # Simplified Chinese
            hant = None  # Traditional Chinese

            if have_zh_page:
                # Case 1: there IS a Chinese (zh) page 
                # Simplified summary: prefer summarizing the Chinese article text.
                if zh_hans_text and len(zh_hans_text) >= MIN_INPUT_CHARS:
                    hans = summarize_zh(zh_hans_text, use_trad=False, main_title=zh_title_hans)
                elif zh_hant_text and len(zh_hant_text) >= MIN_INPUT_CHARS:
                    # If we only have a zh-Hant article, still ask the model to write in Simplified.
                    hans = summarize_zh(zh_hant_text, use_trad=False, main_title=zh_title_hans)
                elif en:
                    # Fallback: no usable Chinese text; translate from English.
                    hans = translate_zh(en, use_trad=False, main_title=zh_title_hans)

                # Traditional summary: ALWAYS derived from the Simplified summary
                # when a Chinese page exists.
                if hans:
                    hant = chat_once(
                        "Convert the following Simplified Chinese text into Traditional Chinese. Do not change meaning.",
                        hans,
                    )
                elif en:
                    # Extremely rare: no Hans at all; last resort is EN -> Traditional.
                    hant = translate_zh(en, use_trad=True, main_title=zh_title_hans)
            else:
                # Case 2: NO Chinese page/link
                # Both Chinese summaries are translated from the English summary.
                if en:
                    hans = translate_zh(en, use_trad=False, main_title=zh_title_hans)
                    hant = translate_zh(en, use_trad=True, main_title=zh_title_hans)

            # Clean up LLM-added note lines like '注：...'
            hans = strip_chinese_notes(hans)
            hant = strip_chinese_notes(hant)

            data["summary_en"] = en
            data["summary_zh_hans"] = hans
            data["summary_zh_hant"] = hant


            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[summarizer] ✅ Saved summary to {out_path}", flush=True)
            wrote += 1

        except Exception as e:
            print(f"[WARN] Failed {json_path}: {e}", flush=True)

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
