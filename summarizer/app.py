#!/usr/bin/env python3
import os, json, time, signal, sys, re
from pathlib import Path
from openai import OpenAI
from typing import Optional, Dict, Tuple


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CLEAN_DIR = DATA_DIR / "clean"
SUMMARY_DIR = DATA_DIR / "summarized"

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://brain:8000/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "local")
MODEL_NAME = os.getenv("LLM_MODEL", "llama-3.1-8b-instruct")
INTERVAL = int(os.getenv("IDLE_INTERVAL", "60"))

client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

SKIP_CATEGORY_DOCS = os.getenv("SUMMARIZER_SKIP_CATEGORIES", "1")  # default: skip
SUMMARIZER_SKIP_LISTS = os.getenv("SUMMARIZER_SKIP_LISTS", "1")
MIN_INPUT_CHARS = int(os.getenv("MIN_INPUT_CHARS", "280"))


def _graceful_exit(signum, frame):
    print("Summarizer shutting down...", flush=True)
    sys.exit(0)


signal.signal(signal.SIGINT, _graceful_exit)
signal.signal(signal.SIGTERM, _graceful_exit)


COMMON_START_WORDS = {
    "The", "A", "An", "In", "On", "At", "For", "From",
    "This", "That", "These", "Those", "It", "He", "She",
}


def normalize_summary(text: str) -> str:
    """
    Clean up obvious markdown-style bullets/headings so TiddlyWiki
    shows simple paragraphs instead of '• ... **'.
    """
    lines = []
    for line in text.splitlines():
        s = line.rstrip()

        # strip leading bullet symbols
        s = s.lstrip()
        s = re.sub(r'^[-*•]\s*', '', s)

        # drop stray trailing '**' used for headings
        s = re.sub(r'\s*\*\*$', '', s)

        lines.append(s)

    out = "\n".join(lines)
    # collapse >2 blank lines
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()


def find_proper_nouns(text: str) -> list[str]:
    """
    Very simple heuristic: grab sequences of Capitalized Words that
    look like people/place names (up to 4 words).
    These are candidates to protect from “creative” translation.
    """
    pattern = re.compile(
        r'\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3})\b'
    )
    results = set()
    for m in pattern.finditer(text):
        phrase = m.group(1)
        first = phrase.split()[0]
        if first in COMMON_START_WORDS:
            continue
        if len(phrase) < 3:
            continue
        results.add(phrase)

    # longest first to avoid nesting replacements
    return sorted(results, key=len, reverse=True)


def protect_terms(
    text: str,
    allow_translate: Optional[set[str]] = None,
) -> Tuple[str, Dict[str, str]]:
    """
    Replace Latin proper nouns with placeholders [[TERM1]], [[TERM2]], ...
    so the LLM can't turn 'Daxinggong Station' into fake Chinese.

    allow_translate: terms we *don't* protect (e.g. main article title
    when we want the model to use a known Chinese name).
    """
    if allow_translate is None:
        allow_translate = set()

    terms = find_proper_nouns(text)
    mapping: Dict[str, str] = {}
    out = text

    for idx, term in enumerate(terms, start=1):
        if term in allow_translate:
            continue
        token = f"[[TERM{idx}]]"
        if token in mapping:
            continue
        mapping[token] = term
        out = re.sub(r'\b' + re.escape(term) + r'\b', token, out)

    return out, mapping


def unprotect_terms(text: str, mapping: Dict[str, str]) -> str:
    """
    Restore placeholders [[TERM1]] back to the original Latin text.
    """
    out = text
    for token, term in mapping.items():
        out = out.replace(token, term)
    return out


def chat_once(system_prompt: str, user_text: str) -> Optional[str]:
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_text[:8000],
                },
            ],
            temperature=0.0,  # keep it deterministic
        )
        raw = resp.choices[0].message.content or ""
        return normalize_summary(raw)
    except Exception as e:
        print(f"[ERROR] LLM call failed: {e}", flush=True)
        return None


def summarize_multi(
    full_text: str,
    title_en: Optional[str] = None,
    zh_title_hans: Optional[str] = None,
    zh_title_hant: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """
    Step 1: Produce ONE clean English summary (no lists, no markdown).
    Step 2: Translate that summary into Simplified & Traditional Chinese.

    Name rules:
      * If zh_title_hans / zh_title_hant are provided, they are the
        canonical Chinese names for the *main subject* and must be used.
      * For other people/place names that appear only in Latin letters
        and have no known Chinese name, we PROTECT them with placeholders
        so the model copies them exactly instead of inventing characters.
    """
    summaries: Dict[str, Optional[str]] = {
        "summary_en": None,
        "summary_zh_hans": None,
        "summary_zh_hant": None,
    }

    # English summary in plain prose
    en_prompt = (
        "You write concise, factual wiki-style summaries under 150 words.\n"
        "Output 3–6 complete sentences of continuous prose.\n"
        "Do NOT use bullet points, numbered lists, section headings, "
        "markdown, or any special formatting. Plain text only.\n"
        "Include all important facts that appear in the input; "
        "do not invent new information."
    )
    en = chat_once(en_prompt, full_text)
    if not en:
        return summaries

    summaries["summary_en"] = en

    # Terms we allow translator to render into Chinese (typically just the title)
    allow_translate: set[str] = set()
    if title_en:
        allow_translate.add(title_en)

    # Protect all other proper nouns with [[TERM#]] placeholders
    protected_en, term_map = protect_terms(en, allow_translate=allow_translate)

    # Simplified Chinese
    hans_prompt = (
        "Translate the following English wiki summary into Simplified Chinese。\n"
        "要求：\n"
        "1. 保留所有信息，不要省略任何重要细节，也不要新增事实。\n"
        "2. 只输出自然的中文段落，不要使用项目符号、标题或任何 Markdown 标记。\n"
        "3. 文本中可能包含形如 [[TERM1]]、[[TERM2]] 的占位符；"
        "请在译文中完整保留这些占位符（包括里面的拉丁字母），"
        "不要翻译或改写它们。\n"
    )
    if zh_title_hans:
        hans_prompt += (
            f"4. 本条目主体在中文维基百科上的规范名称是：“{zh_title_hans}”。"
            "在摘要中提到该主体时，请始终使用这几个汉字作为名称。"
        )

    hans_raw = chat_once(hans_prompt, protected_en)
    if hans_raw:
        hans = unprotect_terms(hans_raw, term_map)
        summaries["summary_zh_hans"] = hans

    # Traditional Chinese
    main_name_hant = zh_title_hant or zh_title_hans
    hant_prompt = (
        "Translate the following English wiki summary into Traditional Chinese。\n"
        "要求：\n"
        "1. 保留所有資訊，不要省略任何重要細節，也不要新增事實。\n"
        "2. 只輸出自然的中文段落，不要使用項目符號、標題或任何 Markdown 標記。\n"
        "3. 文字中可能包含形如 [[TERM1]]、[[TERM2]] 的佔位符；"
        "請在譯文中完整保留這些佔位符（包括裡面的拉丁字母），"
        "不要翻譯或改寫它們。\n"
    )
    if main_name_hant:
        hant_prompt += (
            f"4. 本條目主體在中文維基百科上的規範名稱是：「{main_name_hant}」。"
            "在摘要中提到該主體時，請始終使用這幾個漢字作為名稱。"
        )

    hant_raw = chat_once(hant_prompt, protected_en)
    if hant_raw:
        hant = unprotect_terms(hant_raw, term_map)
        summaries["summary_zh_hant"] = hant

    return summaries


def process_once() -> int:
    """
    Process any new files in /data/clean and write multilingual summaries
    to /data/summarized. Each output JSON mirrors the input plus
    summary_en / summary_zh_hans / summary_zh_hant fields.
    """
    wrote = 0

    for json_path in sorted(CLEAN_DIR.rglob("*.json")):
        out_path = SUMMARY_DIR / json_path.name
        if out_path.exists():
            continue  # already summarized

        data: dict = {}
        text = ""
        url = ""
        doc_type = ""

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))

            url = data.get("url") or ""
            doc_type = (data.get("doc_type") or "").lower()
            text = (data.get("content") or "").strip()

            # Optional fields add in extractor:
            title_en = data.get("title") or None
            zh_title_hans = data.get("zh_title_hans") or None
            zh_title_hant = data.get("zh_title_hant") or None

            # Skip low-value docs
            if (
                not url
                or len(text) < MIN_INPUT_CHARS
                or doc_type in ("disambiguation",)
                or (SKIP_CATEGORY_DOCS == "1" and doc_type == "category")
                or (SUMMARIZER_SKIP_LISTS == "1" and doc_type == "list")
            ):
                print(
                    f"[summarizer] skip {doc_type or 'unknown'} "
                    f"{url or data.get('page_id')} "
                    f"chars={len(text)}<min={MIN_INPUT_CHARS}",
                    flush=True,
                )
                continue

            print(
                f"[summarizer] Summarizing {json_path.relative_to(DATA_DIR)} "
                f"(zh_title_hans={zh_title_hans!r}, zh_title_hant={zh_title_hant!r})",
                flush=True,
            )

            summaries = summarize_multi(
                full_text=text,
                title_en=title_en,
                zh_title_hans=zh_title_hans,
                zh_title_hant=zh_title_hant,
            )

            data["summary_en"] = summaries["summary_en"]
            data["summary_zh_hans"] = summaries["summary_zh_hans"]
            data["summary_zh_hant"] = summaries["summary_zh_hant"]

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            print(f"[summarizer] ✅ Saved summary to {out_path}", flush=True)
            wrote += 1

        except Exception as e:
            print(
                f"[WARN] Failed {json_path}: {e} "
                f"(type={doc_type or '∅'}, url={url or '∅'}, chars={len(text)})",
                flush=True,
            )

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
