#!/usr/bin/env python3
import os, json, time, signal, sys, re
from pathlib import Path
from typing import Optional, Dict, Tuple
from datetime import datetime, timezone  # for last_summarized_at timestamps
from openai import OpenAI

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CLEAN_DIR = DATA_DIR / "clean"
SUMMARY_DIR = DATA_DIR / "summarized"

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://brain:8000/v1")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "local")
MODEL_NAME   = os.getenv("LLM_MODEL", "llama-3.1-8b-instruct")
INTERVAL     = int(os.getenv("IDLE_INTERVAL", "60"))

client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

SKIP_CATEGORY_DOCS      = os.getenv("SUMMARIZER_SKIP_CATEGORIES", "1")
SUMMARIZER_SKIP_LISTS   = os.getenv("SUMMARIZER_SKIP_LISTS", "1")
MIN_INPUT_CHARS         = int(os.getenv("MIN_INPUT_CHARS", "280"))
MAX_LLM_CHARS           = int(os.getenv("MAX_LLM_CHARS", "3500"))

# ===== CHANGE S1: topic_id helper for dedupe across EN/zh variants =====
def derive_topic_id(data: dict, json_path: Path) -> str:
    """
    Derive a stable topic_id for this clean JSON.

    Priority:
    1) explicit topic_id from extractor
    2) zh_title_hans
    3) title
    4) filename stem
    Then normalize to lowercase, underscores, and safe chars.
    """
    raw = (
        (data.get("topic_id") or "").strip()
        or (data.get("zh_title_hans") or "").strip()
        or (data.get("title") or "").strip()
        or json_path.stem
        or ""
    )
    raw = raw.lower()
    raw = re.sub(r"\s+", "_", raw)
    raw = re.sub(r"[^\w\-]+", "", raw)
    return raw or json_path.stem.lower()


# ===== CHANGE S2: collect one best clean doc per topic_id =====
def collect_best_clean_paths() -> Dict[str, Path]:
    """
    Scan CLEAN_DIR and pick at most one JSON per logical topic_id.
    Preference:
      1) documents that have zh article / zh content
      2) latest retrieved_at timestamp
    This prevents multiple zh-variant / duplicate pages from being summarized
    into separate files.
    """
    best: Dict[str, Tuple[Tuple[int, str, str], Path]] = {}

    for json_path in sorted(CLEAN_DIR.rglob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        topic_id = derive_topic_id(data, json_path)

        # has_zh: 1 if this clean record has any zh signals, else 0
        has_zh = 1 if (
            (data.get("zh_url") or "").strip()
            or (data.get("content_zh_hans") or "").strip()
            or (data.get("content_zh_hant") or "").strip()
        ) else 0

        retrieved_at = (data.get("retrieved_at") or "")
        score = (has_zh, retrieved_at, json_path.name)

        prev = best.get(topic_id)
        if (prev is None) or (score > prev[0]):
            best[topic_id] = (score, json_path)

    # unwrap to topic_id -> Path
    return {topic_id: path for topic_id, (score, path) in best.items()}


# helper to remove raw wiki-style links like [[Target]] or [[Target|Label]]
# from the source text before we send it to the LLM.
def strip_wikilinks_markup(text: Optional[str]) -> Optional[str]:
    if not text:
        return text

    def _repl(m: re.Match) -> str:
        inner = m.group(1)
        if "|" in inner:
            return inner.split("|")[-1]
        return inner

    return re.sub(r"\[\[([^\]]+)\]\]", _repl, text)


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
        if stripped.startswith("注：") or stripped.startswith("注意："):
            continue
        keep_lines.append(line)

    cleaned = "\n".join(keep_lines).strip()
    return cleaned or None


# aggressively clean [[...]] markup from LLM output
def cleanup_inline_links(text: Optional[str], lang: str) -> Optional[str]:
    """
    Normalize or strip TiddlyWiki-style [[links]] in model output.

    - Handle [[title|label]] and [[title]].
    - For Chinese summaries, prefer the Chinese part (left side)
      and then aggressively remove any leftover [[ or ]].

    This is intentionally "brute force" so no raw [[...]] shows up
    in rendered tiddlers.
    """
    if not text:
        return text

    # 1) [[title|label]]
    def _repl_pipe(m: re.Match) -> str:
        left, right = m.group(1), m.group(2)
        # For Chinese, keep left (usually Chinese title)
        if lang in ("zh", "zh_hans", "zh-hans", "zh_hant", "zh-hant"):
            return left
        # For English, keep right if present
        return right or left

    text = re.sub(r"\[\[([^|\]]+)\|([^\]]*)\]\]", _repl_pipe, text)

    # 2) [[title]]
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)

    # 3) For Chinese, nuke any stray [[ or ]] that somehow survived
    if lang in ("zh", "zh_hans", "zh-hans", "zh_hant", "zh-hant"):
        text = text.replace("[[", "").replace("]]", "")

    return text



# small relevance classifier for Nanjing-related content
def classify_relevance_with_llm(sample_text: str) -> bool:
    """
    Ask the local LLM whether this article is relevant to Nanjing.
    Returns True if LLM replies 'RELEVANT', False if 'IRRELEVANT',
    and defaults to True on errors/ambiguous output.
    """
    if not sample_text:
        return True  # don't over-filter on completely empty input

    sys_prompt = (
        "You are a filter deciding if an article is about Nanjing, China or "
        "closely related topics (its history, culture, landmarks, people, "
        "institutions, or events).\n"
        "Reply with exactly one word: RELEVANT or IRRELEVANT."
    )
    resp = chat_once(sys_prompt, sample_text[:1500])
    if not resp:
        return True
    answer = resp.strip().upper()
    if "IRRELEVANT" in answer and "RELEVANT" not in answer.replace("IRRELEVANT", ""):
        return False
    if "RELEVANT" in answer:
        return True
    return True  # default to keeping rather than throwing away


def is_relevant_article(
    url: str,
    title: str,
    categories: list[str],
    en_source: str,
    zh_hans_text: str,
    zh_hant_text: str,
) -> bool:
    """
    Combined heuristic + LLM relevance gate for Nanjing topics.
    """
    low_title = (title or "").lower()
    low_url = (url or "").lower()
    cat_text = " ".join(categories or []).lower()

    # Quick heuristics: if we clearly see Nanjing / 南京, keep it.
    if "nanjing" in low_title or "nanjing" in low_url or "nanjing" in cat_text:
        return True
    if "南京" in title or "南京" in (en_source or "") or "南京" in (zh_hans_text or "") or "南京" in (zh_hant_text or ""):
        return True

    # If nothing obviously Nanjing-related, call the LLM on a short sample
    sample = en_source or zh_hans_text or zh_hant_text
    return classify_relevance_with_llm(sample or "")


def _graceful_exit(signum, frame):
    print("Summarizer shutting down...", flush=True)
    sys.exit(0)


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
    script = "Traditional Chinese" if use_trad else "Simplified Chinese"
    sys_prompt = (
        f"根據以下中文資料，用{script}撰寫百科式摘要，3–6句，<180字。"
        "只使用自然段落，不能使用項目符號、標題或任何Markdown標記。"
        "忠實於輸入內容，不要新增事實。"
    )
    if main_title:
        sys_prompt += f" 本條目的中文標題為「{main_title}」，提及主體時請使用此名稱。"
    return chat_once(sys_prompt, source_text)


def translate_zh_from_en(en_summary: str, use_trad: bool, main_title: Optional[str]) -> Optional[str]:
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


def translate_en_from_zh(ch_summary: str) -> Optional[str]:
    return chat_once(
        "Translate the following Chinese encyclopedic summary into natural English. "
        "Keep it concise and factual (3–6 sentences). Plain text only.",
        ch_summary,
    )


def convert_hans_to_hant(hans_text: str) -> Optional[str]:
    return chat_once(
        "Convert the following Simplified Chinese text into Traditional Chinese. "
        "Do not change meaning or add/remove information.",
        hans_text,
    )


def process_once() -> int:
    wrote = 0

    # ===== CHANGE S3: only process one best clean JSON per topic_id =====
    best_paths = collect_best_clean_paths()

    for topic_id, json_path in sorted(best_paths.items()):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[summarizer] skip unreadable clean JSON {json_path}: {e}", flush=True)
            continue

        # ensure topic_id is present in the in-memory dict as well
        topic_id = derive_topic_id(data, json_path)
        data["topic_id"] = topic_id

        # ===== CHANGE S4: summaries named by topic_id, not raw filename =====
        out_path = SUMMARY_DIR / f"{topic_id}.json"

        try:
            url = data.get("url") or ""
            doc_type = (data.get("doc_type") or "").lower()
            lang = (data.get("lang") or "").lower()

            categories = data.get("categories") or []
            derived_tags = set()

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
            }

            for cat in categories:
                tag = CATEGORY_TAG_MAP.get(cat)
                if tag:
                    derived_tags.add(tag)

            # Always keep a generic 'summary' tag too
            derived_tags.add("summary")
            data["tags"] = sorted(derived_tags)

            # ===== CHANGE S5: incremental summarization by content_hash + topic_id =====
            clean_hash = (data.get("content_hash") or "").strip()
            existing = None
            if out_path.exists():
                try:
                    existing = json.loads(out_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = None

            if existing:
                old_hash = (existing.get("content_hash") or "").strip()
                if clean_hash and old_hash and clean_hash == old_hash:
                    # content unchanged → keep old summaries, skip work
                    print(
                        f"[summarizer] unchanged content_hash for topic_id={topic_id}, "
                        f"skipping re-summarize",
                        flush=True,
                    )
                    continue

            if (
                not url
                or doc_type in ("disambiguation",)
                or (SKIP_CATEGORY_DOCS == "1" and doc_type == "category")
                or (SUMMARIZER_SKIP_LISTS == "1" and doc_type == "list")
            ):
                print(f"[summarizer] skip {doc_type or 'unknown'} {url}", flush=True)
                continue

            # LANGUAGE NORMALISATION 
            # Base content fields
            content_main = (data.get("content") or "").strip()
            zh_hans_text = (data.get("content_zh_hans") or "").strip()
            zh_hant_text = (data.get("content_zh_hant") or "").strip()
            zh_title_hans = (data.get("zh_title_hans") or "").strip() or None

            # strip any leftover wiki [[...]] markup from the raw
            # article text before sending it to the LLM.
            content_main = strip_wikilinks_markup(content_main)
            zh_hans_text = strip_wikilinks_markup(zh_hans_text)
            zh_hant_text = strip_wikilinks_markup(zh_hant_text)

            # If this JSON is actually a Chinese page and content_zh_* are empty,
            # treat `content` as Chinese (Simplified) instead of English.
            is_zh_page = (
                lang.startswith("zh")
                or url.startswith("https://zh.wikipedia.org")
            )
            is_en_page = (
                lang.startswith("en")
                or url.startswith("https://en.wikipedia.org")
            )

            if is_zh_page and content_main and not (zh_hans_text or zh_hant_text):
                zh_hans_text = content_main
                content_main = ""  # do not treat as English

            # English source text: only when it's really an English article
            en_source = content_main if is_en_page else ""

            # Short-content guard: if *none* language has enough text, skip
            if (
                len(en_source) < MIN_INPUT_CHARS
                and len(zh_hans_text) < MIN_INPUT_CHARS
                and len(zh_hant_text) < MIN_INPUT_CHARS
            ):
                print(f"[summarizer] too-short content {url}", flush=True)
                continue

            # relevance gate (heuristics + LLM) before we spend tokens summarizing
            title = (data.get("title") or "").strip()
            if not is_relevant_article(
                url=url,
                title=title,
                categories=categories,
                en_source=en_source,
                zh_hans_text=zh_hans_text,
                zh_hant_text=zh_hant_text,
            ):
                print(f"[summarizer] filtered as IRRELEVANT: {url}", flush=True)
                continue

            print(
                f"[summarizer] Summarizing {json_path.relative_to(DATA_DIR)} "
                f"(topic_id={topic_id}, lang={lang or 'unknown'}, zh_url={bool(data.get('zh_url'))})",
                flush=True,
            )

            # MULTILINGUAL LOGIC
            en_summary: Optional[str] = None
            hans_summary: Optional[str] = None
            hant_summary: Optional[str] = None

            have_zh_article = bool(
                (data.get("zh_url") or "").strip()
                or zh_hans_text
                or zh_hant_text
            )

            # 1) Chinese summaries first if there is a zh article
            if have_zh_article:
                # Simplified from zh article (preferred); if only zh-hant exists,
                # we still ask for Simplified output.
                if len(zh_hans_text) >= MIN_INPUT_CHARS:
                    hans_summary = summarize_zh(
                        zh_hans_text, use_trad=False, main_title=zh_title_hans
                    )
                elif len(zh_hant_text) >= MIN_INPUT_CHARS:
                    hans_summary = summarize_zh(
                        zh_hant_text, use_trad=False, main_title=zh_title_hans
                    )

                # Traditional: if we have Hans, convert; otherwise we may later
                # fall back from English.
                if hans_summary:
                    hant_summary = convert_hans_to_hant(hans_summary)

            # 2) English summary
            # Rule: from English article if it exists; otherwise from Chinese.
            if len(en_source) >= MIN_INPUT_CHARS:
                en_summary = summarize_en(en_source)

            if not en_summary:
                # No English article → derive from Chinese summary
                if hans_summary:
                    en_summary = translate_en_from_zh(hans_summary)
                elif hant_summary:
                    en_summary = translate_en_from_zh(hant_summary)

            # 3) If there is NO Chinese article at all, generate Chinese
            # summaries purely by translating the English summary.
            if not have_zh_article:
                if en_summary:
                    if not hans_summary:
                        hans_summary = translate_zh_from_en(
                            en_summary, use_trad=False, main_title=zh_title_hans
                        )
                    if not hant_summary:
                        # For the "no Chinese article" case, spec says:
                        # translate English separately into both Hans and Hant,
                        # not Hans→Hant conversion.
                        hant_summary = translate_zh_from_en(
                            en_summary, use_trad=True, main_title=zh_title_hans
                        )

            else:
                # There *is* a Chinese article but we may still be missing Hans/Hant
                # (e.g. Chinese text too short). In that case, use English summary
                # as the fallback source according to your rules.

                # Simplified Chinese: from zh article when possible, otherwise from EN.
                if not hans_summary and en_summary:
                    hans_summary = translate_zh_from_en(
                        en_summary, use_trad=False, main_title=zh_title_hans
                    )

                # Traditional Chinese:
                # - if we have a Hans summary (from zh article or EN), prefer converting Hans→Hant
                # - otherwise, translate from EN directly.
                if not hant_summary:
                    if hans_summary:
                        hant_summary = convert_hans_to_hant(hans_summary)
                    elif en_summary:
                        hant_summary = translate_zh_from_en(
                            en_summary, use_trad=True, main_title=zh_title_hans
                        )

            # strip any [[...]] from LLM outputs
            en_summary   = cleanup_inline_links(en_summary,   "en")
            hans_summary = cleanup_inline_links(hans_summary, "zh_hans")
            hant_summary = cleanup_inline_links(hant_summary, "zh_hant")

            # Cleanup Chinese note lines
            hans_summary = strip_chinese_notes(hans_summary)
            hant_summary = strip_chinese_notes(hant_summary)

            # Final safety: ensure we *do* have an English summary.
            if not en_summary:
                chinese_source_for_en = hans_summary or hant_summary
                if chinese_source_for_en:
                    en_summary = translate_en_from_zh(chinese_source_for_en)
                    if en_summary:
                        en_summary = en_summary.strip()

            # as a last step, strip any leftover [[...]] markup
            # from summaries themselves, in case the LLM ever emits it.
            en_summary   = strip_wikilinks_markup(en_summary)
            hans_summary = strip_wikilinks_markup(hans_summary)
            hant_summary = strip_wikilinks_markup(hant_summary)

            data["summary_en"] = en_summary
            data["summary_zh_hans"] = hans_summary
            data["summary_zh_hant"] = hant_summary

            # persist content_hash + last_summarized_at into summary JSON
            if clean_hash:
                data["content_hash"] = clean_hash
            data["last_summarized_at"] = datetime.now(timezone.utc).isoformat()

            # keep topic_id in the output JSON for publisher
            data["topic_id"] = topic_id

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"[summarizer] ✅ Saved summary to {out_path}", flush=True)
            wrote += 1

        except Exception as e:
            print(f"[WARN] Failed {json_path}: {e}", flush=True)

    return wrote

# to make automation + services play nicely together
RUN_ONCE = os.getenv("RUN_ONCE") == "1"   # (existing)

if __name__ == "__main__":
    print(f"Summarizer service running... (model={MODEL_NAME})", flush=True)
    print(f"Connecting to LLM at {LLM_BASE_URL}", flush=True)
    try:
        models = client.models.list()
        print(f"LLM reachable, {len(models.data)} models available.", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify LLM: {e}", flush=True)

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    try:  # catch KeyboardInterrupt
        if RUN_ONCE:
            process_once()  # (existing)
        else:
            while True:
                n = process_once()  # (existing)
                if n == 0:
                    time.sleep(INTERVAL)  # (existing)
    except KeyboardInterrupt:
        print("Summarizer interrupted; shutting down...", flush=True)  
    sys.exit(0)
