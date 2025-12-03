#!/usr/bin/env python3
import os, json, time, signal, sys, re
from pathlib import Path
from typing import Optional, Dict, Tuple
from datetime import datetime, timezone  # CHANGE: for last_summarized_at timestamps
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


# CHANGE: aggressively clean [[...]] markup from LLM output
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
# END CHANGE


# CHANGE: small relevance classifier for Nanjing-related content
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
# END CHANGE: relevance classifier

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

    for json_path in sorted(CLEAN_DIR.rglob("*.json")):
        out_path = SUMMARY_DIR / json_path.name
        #if out_path.exists():
        #    continue

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
       

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

            # CHANGE: incremental summarization using content_hash
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
                        f"[summarizer] unchanged content_hash for {url}, "
                        f"skipping re-summarize",
                        flush=True,
                    )
                    continue
            # END CHANGE: incremental summarization

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

            # CHANGE: relevance gate (heuristics + LLM) before we spend tokens summarizing
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
            # END CHANGE: relevance gate

            print(
                f"[summarizer] Summarizing {json_path.relative_to(DATA_DIR)} "
                f"(lang={lang or 'unknown'}, zh_url={bool(data.get('zh_url'))})",
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

            # CHANGE: strip any [[...]] from LLM outputs
            en_summary   = cleanup_inline_links(en_summary,   "en")
            hans_summary = cleanup_inline_links(hans_summary, "zh_hans")
            hant_summary = cleanup_inline_links(hant_summary, "zh_hant")
            # END CHANGE

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

            # CHANGE: as a last step, strip any leftover [[...]] markup
            # from summaries themselves, in case the LLM ever emits it.
            en_summary   = strip_wikilinks_markup(en_summary)
            hans_summary = strip_wikilinks_markup(hans_summary)
            hant_summary = strip_wikilinks_markup(hant_summary)
            # END CHANGE

            data["summary_en"] = en_summary
            data["summary_zh_hans"] = hans_summary
            data["summary_zh_hant"] = hant_summary

            # CHANGE: persist content_hash + last_summarized_at into summary JSON
            if clean_hash:
                data["content_hash"] = clean_hash
            data["last_summarized_at"] = datetime.now(timezone.utc).isoformat()
            # END CHANGE

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
RUN_ONCE = os.getenv("RUN_ONCE") == "1"

if __name__ == "__main__":
    print(f"Summarizer service running... (model={MODEL_NAME})", flush=True)
    print(f"Connecting to LLM at {LLM_BASE_URL}", flush=True)
    try:
        models = client.models.list()
        print(f"LLM reachable, {len(models.data)} models available.", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify LLM: {e}", flush=True)

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    if RUN_ONCE:
        process_once()
        sys.exit(0)
    else:
        while True:
            n = process_once()
            if n == 0:
                time.sleep(INTERVAL)



# Author: Marcelo Villalobos, Juan Quintana, Kate Liu
# **Date: November 2025**

#!/usr/bin/env python3
from enum import auto
import os, json, subprocess, hashlib, re
from pathlib import Path
from datetime import datetime, timezone
import textwrap

# Read environment variables for directories
DATA_DIR     = Path(os.getenv("DATA_DIR", "/data"))
SUMMARY_DIR  = Path(os.getenv("SUMMARY_DIR", str(DATA_DIR / "summarized")))
SITE_DIR     = Path(os.getenv("SITE_DIR", "/site"))
WIKI_WORKDIR = Path(os.getenv("WIKI_WORKDIR", "/tmp/wiki"))

# SPECIAL CASE: all known titles for the tunnel topic                 
TUNNEL_TITLES = {                                                   
    "Nanjing Yingtian Avenue Yangtze River Tunnel",
    "南京应天大街长江隧道",
    "南京應天大街長江隧道",
}

# strip raw wiki-style links like [[Target]] or [[Target|Label]]
# down to plain visible text so we don't carry Wikipedia markup into
# our tiddlers and accidentally generate broken links.
def strip_wikilinks_markup(text: str) -> str:
    if not text:
        return text

    def _repl(m: re.Match) -> str:
        inner = m.group(1)
        # If there's a pipe, keep the *label* (usually the last part).
        if "|" in inner:
            return inner.split("|")[-1]
        return inner

    return re.sub(r"\[\[([^\]]+)\]\]", _repl, text)

# CHANGE: helper to collapse nested wiki-links like [[[[Foo]]]] -> [[Foo]]
def squash_nested_wikilinks(text: str) -> str:
    if not text:
        return text
    # Run a couple of times to catch deeper nesting if any.
    for _ in range(3):
        new_text = re.sub(r"\[\[\s*\[\[([^\]]+)\]\]\s*\]\]", r"[[\1]]", text)
        if new_text == text:
            break
        text = new_text
    return text
# END CHANGE


# Autolink helpers
def build_title_index():
    """
    Scan all summarized JSON files and collect:
    - English titles (for linking English text)
    - Chinese zh-Hans titles (for linking Chinese text)

    Only index titles that actually have at least one non-empty summary,
    so we never autolink to completely missing/empty pages.
    """
    en_titles = []
    zh_titles = []

    for json_path in Path(SUMMARY_DIR).glob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue

        # Only consider items that have at least some summary text
        has_summary = bool(
            (data.get("summary_en") or "").strip()
            or (data.get("summary_zh_hans") or "").strip()
            or (data.get("summary_zh_hant") or "").strip()
        )

        title = (data.get("title") or json_path.stem).strip()

        # normalize the canonical title so that the index matches
        # the final titles we actually use when creating tiddlers.
        raw_en_summary = (data.get("summary_en") or "").strip()

        # Special case: the Yingtian Avenue tunnel should always use the
        # same canonical English title.
        if title in TUNNEL_TITLES:
            title = "Nanjing Yingtian Avenue Yangtze River Tunnel"

        # If the stored title looks Chinese but we *do* have a real English
        # summary, derive an English title from that summary (same logic
        # as in create_tiddlers()) so autolinks point at the correct page.
        elif looks_like_chinese(title) and raw_en_summary and not looks_like_chinese(raw_en_summary):
            derived = derive_english_title_from_summary(raw_en_summary)
            if derived:
                title = derived
        

        if title and has_summary:
            en_titles.append(title)

        zh_title = (data.get("zh_title_hans") or "").strip()
        if zh_title and has_summary:
            zh_titles.append((zh_title, title))

    # Link longer phrases first to avoid shorter ones eating them
    en_titles.sort(key=len, reverse=True)
    zh_titles.sort(key=lambda x: len(x[0]), reverse=True)
    return en_titles, zh_titles


def autolink_en(text: str, en_titles, current_title: str) -> str:
    """
    Turn occurrences of other English titles into [[Title]] links.

    - Only link plain text, not things already inside [[...]].
    """
    if not text:
        return text

    for t in en_titles:
        if t == current_title:
            continue
        # Don't touch occurrences that are already part of a [[wikilink]]
        #   (?<!\[)  → previous character is not '[' (avoids [[Title]])
        #   (?!\])   → next character is not ']'  (avoids [[Title]])
        pattern = r'(?<!\[)\b' + re.escape(t) + r'\b(?!\])'
        text = re.sub(pattern, r'[[\g<0>]]', text)

    return text



def autolink_zh(text: str, zh_titles, current_title: str) -> str:
    """
    Turn occurrences of Chinese titles into <$link> widgets:
      <$link to="EnglishTitle">中文标题</$link>

    We strip any leftover Wikipedia [[...]] markup first so we
    don't ever build nested links.
    """
    if not text:
        return text

    # CHANGE: remove wiki-style [[...]] first so we only ever
    # autolink plain text phrases
    text = strip_wikilinks_markup(text)
    # END CHANGE

    for phrase, canon_title in zh_titles:
        if canon_title == current_title:
            continue
        if phrase in text:
            # CHANGE: use <$link> instead of [[...|...]]
            text = text.replace(
                phrase,
                f'<$link to="{canon_title}">{phrase}</$link>',
            )
            # END CHANGE

    return text



# create url-friendly for filenames
_slug_re = re.compile(r"[^a-z0-9-_]")
def slugify(s: str) -> str:
    s = (s or "untitled").lower().strip().replace(" ", "-")
    s = _slug_re.sub("-", s)
    return re.sub(r"-{2,}", "-", s)[:80]

# ensure Tiddlywiki project structure exists, create a tiddlywiki.info file
def ensure_tw_project():
    (WIKI_WORKDIR / "tiddlers").mkdir(parents=True, exist_ok=True)
    info = {
    "description": "Auto-generated wiki",
    "plugins": [
        "tiddlywiki/tiddlyweb",
        "tiddlywiki/filesystem",
        "tiddlywiki/highlight"
    ],
    "themes": [
        "tiddlywiki/vanilla",
        "tiddlywiki/snowwhite"
    ],
    "languages": [
        "es-ES",
        "fr-FR",
        "en-US",
        "zh-Hans",
        "zh-Hant"
    ],
    "build": {
        "index": [
            "--render",
            "$:/plugins/tiddlywiki/tiddlyweb/save/offline",
            "index.html",
            "text/plain"
        ]
    }
}
    (WIKI_WORKDIR / "tiddlywiki.info").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print("[publisher] Created /tmp/wiki/tiddlywiki.info", flush=True)

# Create a homepage that leads to the wiki site using a search bar.
# Includes inline CSS and JavaScript
def create_homepage():
    html = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>Nanjing Knowledge Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
    :root {
        --bg: #eef1f7;
        --card: #ffffff;
        --accent: #182955;
        --accent-light: #e7ecff;
        --text-muted: #666;
        --border: #d9dce3;
        --shadow: rgba(0,0,0,0.12);
    }

    body {
        margin: 0;
        font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica Neue, Arial;
        background: #f4f4f4;
        color: #222;
        min-height: 100vh;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 20px;
        box-sizing: border-box;
    }

    h1 {
        margin: 0;
        font-size: 48px;
        font-weight: 700;
        color: var(--accent);
        text-align: center;
        letter-spacing: 0.4px;
    }

    hr {
        width: 100%;
        max-width: 650px;
        height: 3px;
        background: var(--accent);
        border: none;
        margin: 8px 0 34px;
        border-radius: 2px;
    }

    .wrapper {
        background: linear-gradient(145deg, #ffffff, #f4f6fb);
        width: 100%;
        max-width: 650px;
        padding: 36px 32px 48px;
        border-radius: 18px;
        box-shadow: 0 4px 20px var(--shadow);
    }

    p.lead {
        margin: 0 0 22px;
        text-align: center;
        color: var(--text-muted);
        font-size: 15px;
    }

    .search-container {
        position: relative;
        width: 100%;
    }

    #search {
        width: 100%;
        padding: 14px 130px 14px 16px;
        font-size: 16px;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: var(--card);
        box-shadow: 0 2px 4px rgba(0,0,0,0.06);
        box-sizing: border-box;
        transition: all 0.2s ease;
    }

    #search:focus {
        outline: none;
        border-color: var(--accent);
        box-shadow: 0 0 0 3px var(--accent-light);
    }

    #openBtn {
        position: absolute;
        right: 6px;
        top: 6px;
        height: calc(100% - 12px);
        padding: 0 18px;
        border-radius: 8px;
        background: var(--accent);
        color: white;
        border: none;
        font-size: 15px;
        cursor: pointer;
        transition: background-color 0.15s ease;
    }

    #openBtn:hover { background: #0f1b38; }
    #openBtn.hidden { opacity: 0; pointer-events: none; }

    #results {
        position: absolute;
        left: 0;
        right: 0;
        top: 54px;
        max-height: 260px;
        overflow-y: auto;
        background: var(--card);
        border-radius: 10px;
        border: 1px solid var(--border);
        box-shadow: 0 6px 16px var(--shadow);
        display: none;
        z-index: 60;
    }

    .result-item {
        padding: 12px 14px;
        cursor: pointer;
        transition: background-color 0.12s ease;
        border-bottom: 1px solid var(--border);
    }

    .result-item:last-child {
        border-bottom: none;
    }

    .result-item:hover,
    .result-item.active {
        background: var(--accent-light);
    }

    .no-results {
        padding: 12px 14px;
        color: var(--text-muted);
        font-style: italic;
    }
</style>
</head>

<body>
<h1>Nanjing Knowledge Hub Wiki</h1>
<hr>
<div class="wrapper">
    <p class="lead">Search articles and open them directly in the wiki.</p>

    <div class="search-container">
        <input id="search" autocomplete="off" placeholder="Start typing a topic…">
        <button id="openBtn" >Go to Wiki</button>
        <div id="results"></div>
    </div>
</div>
<script>

// Load summaries directly from a static file generated by the publisher:
// output/summaries.json

async function loadSummaries() {
    try {
        const res = await fetch("output/summaries.json");
        if (!res.ok) return [];
        const arr = await res.json();
        return Array.isArray(arr) ? arr : [];
    } catch (err) {
        console.warn("Failed to load summaries:", err);
        return [];
    }
}

//UI + exact-match search behavior
let TIDDLERS = [];
let filtered = [];
let activeIndex = -1;

const input = document.getElementById("search");
const results = document.getElementById("results");
const openBtn = document.getElementById("openBtn");


function render() {
    results.innerHTML = "";

    if(!input.value.trim()){
        results.style.display = "none";
        return;
    }

    if(filtered.length === 0){
        results.innerHTML = "<div class='no-results'>No results found</div>";
        results.style.display = "block";
        return;
    }

    filtered.forEach((t, idx) => {
        const div = document.createElement("div");
        div.className = "result-item" + (idx === activeIndex ? " active" : "");
        div.textContent = t.title;
        div.addEventListener("click", () => select(idx));
        results.appendChild(div);
    });

    results.style.display = "block";
    openBtn.classList.remove("hidden");
}

function select(idx) {
    const title = filtered[idx].title;
    const encoded = encodeURIComponent(title);
    window.location.href = `output/index.html#${encoded}`;
}

input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    activeIndex = 0;

    if(!q){
        filtered = [];
        render();
        return;
    }

    // Exact substring match (title OR summary)
    filtered = TIDDLERS.filter(t => {
        const title = (t.title || "").toLowerCase();
        const summary = (t.summary || "").toLowerCase();

        return title.includes(q) || summary.includes(q);
    });
    render();
});

input.addEventListener("keydown", e => {
    if(!["ArrowDown","ArrowUp","Enter","Escape"].includes(e.key)) return;

    if(e.key === "ArrowDown"){
        e.preventDefault();
        if(filtered.length === 0) return;
        activeIndex = Math.min(activeIndex + 1, filtered.length - 1);
        highlightScroll();
    }
    if(e.key === "ArrowUp"){
        e.preventDefault();
        if(filtered.length === 0) return;
        activeIndex = Math.max(activeIndex - 1, 0);
        highlightScroll();
    }
    if(e.key === "Enter"){
        e.preventDefault();
        if(activeIndex >= 0 && filtered.length) select(activeIndex);
    }
    if(e.key === "Escape"){
        results.style.display = "none";
    }
});

function highlightScroll(){
    const items = results.children;
    for(let i = 0; i < items.length; i++){
        items[i].classList.toggle("active", i === activeIndex);
    }
    const el = items[activeIndex];
    if(el) el.scrollIntoView({block: "nearest"});
}

openBtn.addEventListener("click", () => {
    if (filtered.length && activeIndex >= 0) {
        select(activeIndex);
    } else {
        // No specific article chosen → just open the wiki
        window.location.href = "output/index.html";
    }
});

document.addEventListener("click", ev => {
    if(!ev.target.closest(".search-container")){
        results.style.display = "none";
    }
});

// Start loading summaries from static file
(async () => {
    TIDDLERS = await loadSummaries();
})();

</script>

</body>
</html>
"""
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")
    print("[publisher] Created homepage with search function.")

# Plugin for use with homepage, opens advance search to find tiddler.
def inject_search_handler():

    plugin_dir = WIKI_WORKDIR / "tiddlers" / "plugins" / "external-search"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    tiddler_path = plugin_dir / "startup.tid"
    content = """
title: $:/plugins/external-search/startup
type: application/javascript
module-type: startup

(function(){

exports.name = "external-search-startup";
exports.after = ["startup"];
exports.platforms = ["browser"];

exports.startup = function() {

    // TW only reads the hash AFTER '#' — no '?', no '&'
    var hash = window.location.hash || "";

    // look for pattern #extsearch:Topic
    if(!hash.startsWith("#extsearch:")) return;

    var query = decodeURIComponent(hash.substring("#extsearch:".length)).trim();
    if(!query) return;

    console.log("[ExternalSearch] Searching:", query);

    // Open AdvancedSearch panel, "search" tab
    $tw.wiki.setText("$:/temp/AdvancedSearch/Tab","text",null,"search");

    // Set search query
    $tw.wiki.setText("$:/temp/AdvancedSearch/Input","text",null,query);

    // Get matching tiddlers
    var results = $tw.wiki.filterTiddlers("[search[" + query + "]]");

    // If exactly 1 → auto-open
    if(results.length === 1) {
        $tw.rootWidget.invokeActionString(
            "<$action-navigate $to='" + results[0] + "' />"
        );
    }
};
})();
"""
    tiddler_path.write_text(content.strip(), encoding="utf-8")
    print("[publisher] Injected external search handler", flush=True)

# HELPERS FOR LANGUAGE HEURISTICS AND TITLE DERIVATION
def looks_like_chinese(text: str) -> bool:   
    """Return True if text looks like it's mostly CJK characters."""  
    if not text:                                                     
        return False                                                 
    cjk = 0                                                          
    for ch in text:                                                  
        if "\u4e00" <= ch <= "\u9fff":                               
            cjk += 1                                                 
    # "mostly Chinese" = at least 4 CJK chars and > 25% of all chars  
    return cjk >= 4 and cjk > len(text) / 4.0                        


def derive_english_title_from_summary(en_summary: str) -> str | None:  
    """Try to pull an English-sounding title from the first part of     
    the English summary, e.g. 'Nanjing Industrial University Station     
    is a metro station...' -> 'Nanjing Industrial University Station'."""  
    if not en_summary:                                                  
        return None                                                     
    text = en_summary.strip()                                           
    # Look for '... is', '... was', comma, or period as a first break   
    m = re.match(r"^(.+?)(?:\s+is\b|\s+was\b|,|\.)", text)              
    if m:                                                               
        candidate = m.group(1).strip()                                  
    else:                                                               
        candidate = text[:80].strip()                                   
    if sum(1 for ch in candidate if ch.isalpha()) < 4:                  
        return None                                                     
    return candidate                                                    


# create tiddlers from JSON summaries, build .tid files
def create_tiddlers(en_titles, zh_titles) -> int:
    """
    Read all summarized JSON files and turn them into .tid tiddlers.

    UPDATED BEHAVIOUR:
      1) First pass groups JSON files by "topic".
      2) For each topic, we pick ONE best JSON:
         - Prefer one that has a non-empty, non-Chinese summary_en.
      3) Second pass writes exactly one tiddler per topic.
         - If title is Chinese but summary_en is English, we derive
           an English title from the summary.
         - If summary_en is actually Chinese, we treat it as missing
           for English so there is NO Chinese body when language=English.
    """
    tiddlers_dir = WIKI_WORKDIR / "tiddlers"
    tiddlers_dir.mkdir(parents=True, exist_ok=True)

    # FIRST PASS — choose ONE best JSON per topic                        
    topics = {}  # topics[topic_key] = {"data": <json dict>, "json_name": "..."}   

    for json_path in Path(SUMMARY_DIR).glob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"[WARN] failed to read {json_path.name}: {e}", flush=True)
            continue

        # Normalize base title (remove [[ ]] if present)
        raw_title = (data.get("title") or json_path.stem).strip()
        m = re.match(r"^\[\[(.+?)\]\]$", raw_title)
        if m:
            title = m.group(1).strip()
        else:
            title = raw_title

        # Normalize Chinese titles (strip [[ ]] if present)
        raw_zh_hans = (data.get("zh_title_hans") or "").strip()
        m_hans = re.match(r"^\[\[(.+?)\]\]$", raw_zh_hans)
        zh_title_hans = m_hans.group(1).strip() if m_hans else raw_zh_hans

        raw_zh_hant = (data.get("zh_title_hant") or "").strip()
        m_hant = re.match(r"^\[\[(.+?)\]\]$", raw_zh_hant)
        zh_title_hant = m_hant.group(1).strip() if m_hant else raw_zh_hant

        if zh_title_hans and not zh_title_hant:
            zh_title_hant = zh_title_hans

        # Decide a "topic key" used for grouping JSONs that are the same entity
        if (
            title in TUNNEL_TITLES
            or zh_title_hans in TUNNEL_TITLES
            or zh_title_hant in TUNNEL_TITLES
        ):
            topic_key = "Nanjing Yingtian Avenue Yangtze River Tunnel"    
        else:
            topic_key = title

        # Check whether THIS JSON has an apparently-English summary_en         
        raw_en_summary = (data.get("summary_en") or "").strip()               
        candidate_has_en = bool(raw_en_summary and not looks_like_chinese(raw_en_summary))   

        existing = topics.get(topic_key)
        if not existing:
            # First time we see this topic → keep it                           
            topics[topic_key] = {
                "data": data,
                "json_name": json_path.name,
            }
        else:
            # Prefer a JSON that has real English summary_en                   
            existing_raw_en = (existing["data"].get("summary_en") or "").strip()
            existing_has_en = bool(existing_raw_en and not looks_like_chinese(existing_raw_en))
            if not existing_has_en and candidate_has_en:
                print(
                    f"[publisher] For topic '{topic_key}', "
                    f"preferring {json_path.name} (has English summary) "
                    f"over {existing['json_name']}",
                    flush=True,
                )
                topics[topic_key] = {
                    "data": data,
                    "json_name": json_path.name,
                }
            # else: keep existing                                              

    # SECOND PASS — actually write one tiddler per topic                  
    count = 0

    for topic_key, entry in topics.items():
        data = entry["data"]
        json_name = entry["json_name"]

        try:
            # NORMALISE ENGLISH TITLE (strip [[ ]] if present) 
            raw_title = (data.get("title") or topic_key).strip()
            m = re.match(r"^\[\[(.+?)\]\]$", raw_title)
            if m:
                title = m.group(1).strip()
            else:
                title = raw_title

            # NORMALISE CHINESE TITLES  
            raw_zh_hans = (data.get("zh_title_hans") or "").strip()
            m_hans = re.match(r"^\[\[(.+?)\]\]$", raw_zh_hans)
            zh_title_hans = m_hans.group(1).strip() if m_hans else raw_zh_hans

            raw_zh_hant = (data.get("zh_title_hant") or "").strip()
            m_hant = re.match(r"^\[\[(.+?)\]\]$", raw_zh_hant)
            zh_title_hant = m_hant.group(1).strip() if m_hant else raw_zh_hant

            if zh_title_hans and not zh_title_hant:
                zh_title_hant = zh_title_hans

            # SPECIAL CASE: tunnel topic canonicalisation  
            if topic_key == "Nanjing Yingtian Avenue Yangtze River Tunnel":
                title = "Nanjing Yingtian Avenue Yangtze River Tunnel"
                if not zh_title_hans:
                    zh_title_hans = "南京应天大街长江隧道"
                if not zh_title_hant:
                    zh_title_hant = "南京應天大街長江隧道"

            # SUMMARIES  
            en_summary   = (data.get("summary_en") or "").strip()
            hans_summary = (data.get("summary_zh_hans") or "").strip()
            hant_summary = (data.get("summary_zh_hant") or "").strip()

            # strip raw wiki [[...]] markup from summaries so it
            # doesn't create visible brackets or broken internal links.
            en_summary   = strip_wikilinks_markup(en_summary)
            hans_summary = strip_wikilinks_markup(hans_summary)
            hant_summary = strip_wikilinks_markup(hant_summary)
            

            # If "English" summary is actually Chinese, treat it as missing    
            if en_summary and looks_like_chinese(en_summary):                  
                print(f"[publisher] summary_en looks Chinese for '{title}', disabling English body", flush=True)   
                en_summary = ""                                                

            # If title is Chinese-looking but we now have an English summary,
            # derive an English title from the summary (e.g. the station case).   
            if looks_like_chinese(title) and en_summary:                       
                derived = derive_english_title_from_summary(en_summary)        
                if derived:                                                    
                    print(f"[publisher] Using derived English title '{derived}' for topic '{topic_key}' (was '{title}')", flush=True)   
                    title = derived                                            

            # INTERNAL AUTOLINKING  
            en_linked   = autolink_en(en_summary,   en_titles, title)
            hans_linked = autolink_zh(hans_summary, zh_titles, title)
            hant_linked = autolink_zh(hant_summary, zh_titles, title)

            # Mark if this article actually has usable English content
            has_en = "yes" if en_summary else "no"    

            # CHANGE: pull timing metadata from summarizer output
            retrieved_at = (data.get("retrieved_at") or "").strip()
            last_summarized_at = (data.get("last_summarized_at") or "").strip()
            # END CHANGE                          

            # Language-aware body: EN / zh-Hans / zh-Hant
            body = textwrap.dedent(f"""
            <$list filter="[[$:/state/wiki-language]get[text]match[en]]">
            {en_linked}
            </$list>

            <$list filter="[[$:/state/wiki-language]get[text]match[zh-hans]]">
            {hans_linked}
            </$list>

            <$list filter="[[$:/state/wiki-language]get[text]match[zh-hant]]">
            {hant_linked}
            </$list>
            """).strip()

            # CHANGE: as a final safety net, collapse any nested wiki-links
            # that might still exist in the combined body, e.g. [[[[Foo]]]]
            # → [[Foo]]. TiddlyWiki will then render them as normal links.
            body = squash_nested_wikilinks(body)
            # END CHANGE

            # NOTE: we do NOT fall back to generic text here, because that      
            # might be Chinese; when language=English and en_summary is empty
            # we prefer to show nothing over showing Chinese text by mistake.

            # TAGS (drop 'summary' + empties)  
            raw_tags = data.get("tags") or []
            tags = [t for t in raw_tags if t and t != "summary"]
            tagstr = " ".join(tags)

            # SOURCES  
            en_source = (data.get("url") or "").strip()
            zh_source = (data.get("zh_url") or "").strip()

            created = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            sid     = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
            fname   = f"{slugify(title)}-{sid}.tid"

            source_parts = []
            if en_source:
                source_parts.append(f"[[{en_source}]]")
            if zh_source and (hans_summary or hant_summary):
                source_parts.append(f"[[{zh_source}]]")
            source_line = "source: " + (" ; ".join(source_parts) if source_parts else "unknown")

            # HEADER FIELDS  
            header_lines = [
                f"title: {title}",
                f"tags: {tagstr}",
                "type: text/vnd.tiddlywiki",
                f"created: {created}",
                f"modified: {created}",
                f"has_en: {has_en}",
            ]
            if zh_title_hans:
                header_lines.append(f"zh_title_hans: {zh_title_hans}")
            if zh_title_hant:
                header_lines.append(f"zh_title_hant: {zh_title_hant}")
            if retrieved_at:
                header_lines.append(f"retrieved_at: {retrieved_at}")
            if last_summarized_at:
                header_lines.append(f"last_summarized_at: {last_summarized_at}")

            header = "\n".join(header_lines)

            # CHANGE: visible metadata footer inside the tiddler body
            meta_parts = []
            if retrieved_at:
                meta_parts.append(f"retrieved: {retrieved_at}")
            if last_summarized_at:
                meta_parts.append(f"summarized: {last_summarized_at}")
            meta_line = "meta: " + " ; ".join(meta_parts) if meta_parts else ""
            # END CHANGE

            tid = f"{header}\n\n{body}\n\n{source_line}\n"

            (tiddlers_dir / fname).write_text(tid, encoding="utf-8")
            count += 1

        except Exception as e:
            print(f"[WARN] failed {json_name} for topic '{topic_key}': {e}", flush=True)

    print(f"[publisher] Created {count} tiddlers from {SUMMARY_DIR}")
    return count


# Generate a single static summaries file for the homepage to load directly
def generate_summaries_output():
    entries = []
    for f in SUMMARY_DIR.glob("*.json"):
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            title = data.get("title") or f.stem
            summary = data.get("summary_en") or data.get("summary") or ""
            entries.append({
                "title": title,
                "summary": summary
            })
        except Exception as e:
            print(f"[WARN] skipping {f.name}: {e}", flush=True)

    entries.sort(key=lambda x: x["title"].lower())

    out = SITE_DIR / "output"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "summaries.json"
    dest.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[publisher] Wrote summaries output to {dest} ({len(entries)} entries)")

def create_tag_tiddlers():
    """
    Create one Tag definition tiddler per Chinese tag.

    Each tag tiddler:
      - title:   <Chinese tag>  (must match the tag on articles)
      - tags:    $:/tags/Tag    (so TW treats it as a tag)
      - fields:  caption-en, caption-zh-hans, caption-zh-hant
    """
    tiddlers_dir = WIKI_WORKDIR / "tiddlers"
    tiddlers_dir.mkdir(parents=True, exist_ok=True)

    # Chinese tag -> (English label, Simplified, Traditional)
    TAG_LABELS = {
        "景点": ("Tourist attractions in Nanjing", "景点", "景點"),
        "历史": ("History of Nanjing", "历史", "歷史"),
        "美食": ("Cuisine of Nanjing", "美食", "美食"),
        "公园": ("Parks in Nanjing", "公园", "公園"),
        "博物馆": ("Museums in Nanjing", "博物馆", "博物館"),
        "高校": ("Universities and colleges in Nanjing", "高校", "高校"),
        "体育": ("Sports in Nanjing", "体育", "體育"),
        "交通": ("Transportation in Nanjing", "交通", "交通"),
        "经济": ("Economy of Nanjing", "经济", "經濟"),
        "文化": ("Culture in Nanjing", "文化", "文化"),
        "地理": ("Geography of Nanjing", "地理", "地理"),
        "历史遗迹": ("Historic sites in Nanjing", "历史遗迹", "歷史遺跡"),
        "媒体": ("Mass media in Nanjing", "媒体", "媒體"),
        "宗教": ("Religion in Nanjing", "宗教", "宗教"),
        "政府": ("Government of Nanjing", "政府", "政府"),
        "南京": ("Nanjing", "南京", "南京"),
        "建筑": ("Buildings and structures in Nanjing", "建筑", "建築"),
        "事件": ("Events in Nanjing", "事件", "事件"),
        "艺术": ("Arts in Nanjing", "艺术", "藝術"),
        "科技": ("Science and technology in Nanjing", "科技", "科技"),
        "名人": ("Notable people from Nanjing", "名人", "名人"),
        "公司": ("Companies based in Nanjing", "公司", "公司"),
        "医院": ("Hospitals in Nanjing", "医院", "醫院"),
        "桥梁": ("Bridges in Nanjing", "桥梁", "橋樑"),
        "街道": ("Streets in Nanjing", "街道", "街道"),
        "河流": ("Rivers of Nanjing", "河流", "河流"),
        "湖泊": ("Lakes of Nanjing", "湖泊", "湖泊"),
        "山脉": ("Mountains of Nanjing", "山脉", "山脈"),
        "节日": ("Festivals in Nanjing", "节日", "節日"),
        "旅游": ("Tourism in Nanjing", "旅游", "旅遊"),
    }

    # Discover which tags actually appear in summarized JSON
    used_tags = set()
    for json_path in SUMMARY_DIR.glob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        for tag in data.get("tags") or []:
            tag = (tag or "").strip()           
            if not tag or tag == "summary":    
                continue
            used_tags.add(tag)


    if not used_tags:
        print("[publisher] No tags found in summaries; skipping tag tiddlers", flush=True)
        return

    # Create one tag tiddler per used tag
    count = 0
    for tag in sorted(used_tags):
        cfg = TAG_LABELS.get(tag)
        if cfg:
            en_label, zh_hans_label, zh_hant_label = cfg
        else:
            # Fallback: show the raw tag for all languages
            en_label = zh_hans_label = zh_hant_label = tag

        header_lines = [
            f"title: {tag}",
            "tags: $:/tags/Tag excludeLists",
            "type: text/vnd.tiddlywiki",
            f"caption-en: {en_label}",
            f"caption-zh-hans: {zh_hans_label}",
            f"caption-zh-hant: {zh_hant_label}",
        ]
        header = "\n".join(header_lines)

        # Small body fallback; normally tag pills will use popup instead
        body = "<<lang-tag-caption>>"

        # Filename: hash the tag so we don't fight with non-ASCII and slashes
        fname = f"__tag-{hashlib.sha1(tag.encode('utf-8')).hexdigest()[:8]}.tid"
        tid_text = header + "\n\n" + body + "\n"
        (tiddlers_dir / fname).write_text(tid_text, encoding="utf-8")
        count += 1

    print(f"[publisher] Created {count} tag tiddlers", flush=True)


# Create $:/SiteTitle and $:/SiteSubtitle tiddlers for Headings
# Inject global language state and language switcher tiddlers,
# so users can switch languages in the wiki UI.
def inject_tiddlers():
    tiddlers_dir = WIKI_WORKDIR / "tiddlers"
    tiddlers_dir.mkdir(parents=True, exist_ok=True)

    # Site title + subtitle (language aware) 
    site_title = textwrap.dedent("""
    title: $:/SiteTitle
    type: text/vnd.tiddlywiki

    <$list filter="[[$:/state/wiki-language]get[text]match[zh-hans]]">
    南京知识枢纽维基
    </$list>

    <$list filter="[[$:/state/wiki-language]get[text]match[zh-hant]]">
    南京知識樞紐維基
    </$list>

    <$list filter="[[$:/state/wiki-language]get[text]match[en]]">
    Nanjing Knowledge Hub Wiki
    </$list>
    """).strip()

    site_subtitle = textwrap.dedent("""
    title: $:/SiteSubtitle
    type: text/vnd.tiddlywiki

    <$list filter="[[$:/state/wiki-language]get[text]match[zh-hans]]">
    南京小百科，浓浓鸭子味儿
    </$list>

    <$list filter="[[$:/state/wiki-language]get[text]match[zh-hant]]">
    南京小百科，濃濃鴨子味兒
    </$list>

    <$list filter="[[$:/state/wiki-language]get[text]match[en]]">
    Nanjing Encyclopedia, with a strong duck flavor
    </$list>
    """).strip()

    # language state + picker
    lang_state = textwrap.dedent("""
    title: $:/state/wiki-language
    type: text/vnd.tiddlywiki

    en
    """).strip()

    lang_switcher = textwrap.dedent("""
    title: Language
    tags: $:/tags/PageControls
    type: text/vnd.tiddlywiki

    <span class="tc-language-picker">
    🌐 <$text text="Language / 语言：" />
    <$select tiddler="$:/state/wiki-language">
        <option value="zh-hans">简体中文</option>
        <option value="zh-hant">繁體中文</option>
        <option value="en">English</option>
    </$select>
    </span>
    """).strip()

    # Macros 
    lang_macros = textwrap.dedent("""
    title: $:/plugins/wiki/lang-macros
    tags: $:/tags/Macro
    type: text/vnd.tiddlywiki

    \define lang-caption()
    <$reveal type="match" state="$:/state/wiki-language" text="zh-hans">
      <$view field="zh_title_hans" default=<<view field "title">> />
    </$reveal>

    <$reveal type="match" state="$:/state/wiki-language" text="zh-hant">
      <$view field="zh_title_hant" default=<<view field "title">> />
    </$reveal>

    <$reveal type="match" state="$:/state/wiki-language" text="en">
      <$view field="title" />
    </$reveal>
    \end
    """).strip()

    tag_label_macro = textwrap.dedent("""
    title: $:/plugins/wiki/tag-label-macro
    tags: $:/tags/Macro
    type: text/vnd.tiddlywiki

    \define lang-tag-caption()
    <$reveal type="match" state="$:/state/wiki-language" text="zh-hans">
      <$view field="caption-zh-hans" default=<<view field "title">> />
    </$reveal>

    <$reveal type="match" state="$:/state/wiki-language" text="zh-hant">
      <$view field="caption-zh-hant" default=<<view field "title">> />
    </$reveal>

    <$reveal type="match" state="$:/state/wiki-language" text="en">
      <$view field="caption-en" default=<<view field "title">> />
    </$reveal>
    \end
    """).strip()

    # List items & titles 
    list_item = textwrap.dedent("""
    title: $:/core/ui/ListItemTemplate
    type: text/vnd.tiddlywiki

    <div class="tc-menu-list-item">
    <$link to={{!!title}}>
      <<lang-caption>>
    </$link>
    </div>
    """).strip()
   
    title_default = textwrap.dedent("""
    title: $:/core/ui/ViewTemplate/title/default
    type: text/vnd.tiddlywiki

    \whitespace trim
    <h2 class="tc-title">
    <$reveal type="match" state="$:/state/wiki-language" text="zh-hans">
      <$view field="zh_title_hans" default=<<view field "title">> />
    </$reveal>
    <$reveal type="match" state="$:/state/wiki-language" text="zh-hant">
      <$view field="zh_title_hant" default=<<view field "title">> />
    </$reveal>
    <$reveal type="match" state="$:/state/wiki-language" text="en">
      <$view field="title" />
    </$reveal>
    </h2>
    """).strip()

    # Welcome tiddler
    welcome_tiddler = textwrap.dedent("""
    title: Welcome to the Nanjing Knowledge Hub Wiki
    type: text/vnd.tiddlywiki
    zh_title_hans: 欢迎来到南京知识枢纽维基
    zh_title_hant: 歡迎來到南京知識樞紐維基

    <$reveal type="match" state="$:/state/wiki-language" text="zh-hans">
    本维基致力于提供关于南京的全面信息。南京是中国一座充满活力的城市，
    以其悠久的历史、丰富的文化和现代化的发展而闻名。在这里，
    您可以找到涵盖南京各个方面的维基百科文章摘要，包括地标建筑、教育机构、
    文化活动等等。浏览这些文章，了解这座城市的历史遗产，
    并随时掌握这座充满活力的国际大都市的最新动态。
    </$reveal>

    <$reveal type="match" state="$:/state/wiki-language" text="zh-hant">
    本維基致力於提供關於南京的全面資訊。南京是中國一個充滿活力的城市，
    以其悠久的歷史、豐富的文化和現代化的發展而聞名。在這裡，
    您可以找到涵蓋南京各個方面的維基百科文章摘要，包括地標建築、教育機構、
    文化活動等等。瀏覽這些文章，了解這座城市的歷史遺產，
    並隨時掌握這座充滿活力的國際大都市的最新動態。
    </$reveal>

    <$reveal type="match" state="$:/state/wiki-language" text="en">
    This wiki is dedicated to providing comprehensive information about Nanjing,
    a vibrant city in China known for its rich history, culture, and modern development.
    Here, you will find summarized Wikipedia articles covering various aspects of Nanjing,
    including its landmarks, educational institutions, cultural events, and more.
    Explore the articles, learn about the city's heritage, and stay updated with the latest
    developments in this dynamic metropolis.
    </$reveal>
    """).strip()

    default_tiddlers = textwrap.dedent("""
    title: $:/DefaultTiddlers
    type: text/vnd.tiddlywiki

    [[Welcome to the Nanjing Knowledge Hub Wiki]]
    """).strip()


    recent_sidebar = textwrap.dedent("""
    title: $:/core/ui/SideBar/Recent
    tags: $:/tags/SideBar
    caption: {{$:/language/SideBar/Recent/Caption}}
    list-after: $:/core/ui/SideBar/Open
    type: text/vnd.tiddlywiki

    \whitespace trim
    <div class="tc-sidebar-lists tc-recent-list">

      <!-- Language-aware date heading -->
      <div class="nj-recent-date">
        <$reveal type="match" state="$:/state/wiki-language" text="en">
          <$macrocall $name="now" format={{$:/language/RecentChanges/DateFormat}}/>
        </$reveal>

        <$reveal type="match" state="$:/state/wiki-language" text="zh-hans">
          <$macrocall $name="now" format="YYYY年0MM月0DD日"/>
        </$reveal>

        <$reveal type="match" state="$:/state/wiki-language" text="zh-hant">
          <$macrocall $name="now" format="YYYY年0MM月0DD日"/>
        </$reveal>
      </div>

      <!-- When English is selected, only show pages that have English content -->
      <$reveal type="match" state="$:/state/wiki-language" text="en">
        <$list filter="[all[tiddlers]!is[system]!has[draft.of]!tag[excludeLists]field:has_en[yes]sort[modified]reverse[]limit[50]]">
          <div class="tc-menu-list-item">
            <$link to=<<currentTiddler>>>
              <<lang-caption>>
            </$link>
          </div>
        </$list>
      </$reveal>

      <!-- For Chinese UI, show all recent pages (even if they don't have English) -->
      <$reveal type="nomatch" state="$:/state/wiki-language" text="en">
        <$list filter="[all[tiddlers]!is[system]!has[draft.of]!tag[excludeLists]sort[modified]reverse[]limit[50]]">
          <div class="tc-menu-list-item">
            <$link to=<<currentTiddler>>>
              <<lang-caption>>
            </$link>
          </div>
        </$list>
      </$reveal>

    </div>
    """).strip()


    tag_template = textwrap.dedent("""
    title: $:/core/ui/TagTemplate
    type: text/vnd.tiddlywiki

    \whitespace trim
    <$list filter="[<currentTiddler>regexp[\S]]">
    <div class="tc-tag-list-item nj-tag-holder">

      <!-- OPEN state: pill + dropdown; clicking pill closes -->
      <$reveal type="match"
               state="$:/state/nj-open-tag"
               text=<<qualify "tag-">> >

        <$button class="tc-btn-invisible nj-tag-pill nj-tag-pill-open"
                 set="$:/state/nj-open-tag"
                 setTo="">
          <span class="nj-tag-label"><<lang-tag-caption>></span>
        </$button>

        <div class="nj-tag-popup">
          <div class="nj-tag-popup-header"><<lang-tag-caption>></div>
          <div class="nj-tag-popup-body">
            <$list filter="[tag<currentTiddler>sort[title]]">
              <div class="nj-tag-popup-item">
                <$link to=<<currentTiddler>>>
                  <<lang-caption>>
                </$link>
              </div>
            </$list>
          </div>
        </div>
      </$reveal>

      <!-- CLOSED state: simple pill; clicking opens -->
      <$reveal type="nomatch"
               state="$:/state/nj-open-tag"
               text=<<qualify "tag-">> >
        <$button class="tc-btn-invisible nj-tag-pill"
                 set="$:/state/nj-open-tag"
                 setTo=<<qualify "tag-">> >
          <span class="nj-tag-label"><<lang-tag-caption>></span>
        </$button>
      </$reveal>

    </div>
    </$list>
    """).strip()


    tag_clickoutside_startup = textwrap.dedent("""
    title: $:/plugins/wiki/tag-clickoutside-startup
    type: application/javascript
    module-type: startup

    (function(){

    exports.name = "nj-tag-close-click-outside";
    exports.after = ["startup"];
    exports.platforms = ["browser"];

    exports.startup = function() {
      document.addEventListener("click", function(event) {
        // If no tag dropdown is open, nothing to do
        var openTag = $tw.wiki.getTiddlerText("$:/state/nj-open-tag","");
        if(!openTag) {
          return;
        }

        // Walk up from the clicked element; if we hit a .nj-tag-holder,
        // the click is inside the tag pill/popup → don't auto-close.
        var el = event.target;
        while(el) {
          if(el.classList && el.classList.contains("nj-tag-holder")) {
            return;
          }
          el = el.parentElement;
        }

        // Clicked outside any tag-holder: close the dropdown.
        // This does NOT cancel the click itself; links still navigate.
        $tw.wiki.setText("$:/state/nj-open-tag","text",null,"");
      }, true);
    };

    })();
    """).strip()


    tag_styles = textwrap.dedent("""
    title: $:/plugins/wiki/tag-styles
    tags: $:/tags/Stylesheet
    type: text/vnd.tiddlywiki

    /* Container for a single tag pill + its dropdown */
    .nj-tag-holder {
      position: relative;
      display: inline-block;
    }

    /* Yellow "chip" like the sample site */
    .nj-tag-pill {
      background: #f7c948;
      border-radius: 999px;
      padding: 4px 14px;
      margin: 4px 6px 0 0;
      display: inline-flex;
      align-items: center;
      cursor: pointer;
    }

    .nj-tag-pill-open {
      /* optional subtle change when open */
      box-shadow: 0 0 0 2px rgba(247,201,72,0.4);
    }

    .nj-tag-label {
      font-size: 0.9em;
      font-weight: 500;
      white-space: nowrap;
    }

    /* <<< hide any tag pill whose label is empty >>> */
    .nj-tag-pill:has(.nj-tag-label:empty) {
      display: none;
    }

    /* Full-screen click-away scrim (behind popup, above page) */
    .nj-tag-scrim {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 1000;
    }

    .nj-tag-scrim-btn {
      width: 100%;
      height: 100%;
      padding: 0;
      margin: 0;
      border: 0;
      background: transparent;
      cursor: default;
    }

    /* The dropdown itself; floats under the tag, doesn't push content */
    .nj-tag-popup {
      position: absolute;
      top: calc(100% + 6px);
      left: 0;
      min-width: 260px;
      max-width: 360px;
      box-shadow: 0 4px 14px rgba(0,0,0,0.18);
      border-radius: 8px;
      overflow: hidden;
      background: #ffffff;
      z-index: 1001; /* above scrim */
    }

    .nj-tag-popup-header {
      padding: 6px 10px;
      background: #4c6fff;
      color: #fff;
      font-weight: 600;
      font-size: 0.9em;
    }

    .nj-tag-popup-body {
      max-height: 260px;   /* scroll if many pages */
      overflow-y: auto;
      background: #fff;
    }

    .nj-tag-popup-item {
      padding: 6px 10px;
    }

    .nj-tag-popup-item a {
      color: #3366cc;
      text-decoration: none;
    }

    .nj-tag-popup-item a:hover {
      text-decoration: underline;
      background: #f5f7ff;
    }

    /* Recent-tab date heading */
    .nj-recent-date {
      font-size: 0.85em;
      font-weight: 600;
      color: #888;
      margin: 0 0 0.4em 0;
    }

    """).strip()


    # Ensure Tag Manager does NOT show an empty first bullet when the tag name
    # is blank. We only render a <li> when currentTiddler is non-empty.   
    tagmanager_listitem = textwrap.dedent("""
    title: $:/plugins/tiddlywiki/tag-manager/ui/TagListItemTemplate
    type: text/vnd.tiddlywiki

    \whitespace trim
    <$list filter="[<currentTiddler>regexp[\S]]">
    <li>
      <$link to=<<currentTiddler>>>
        <<lang-tag-caption>>
      </$link>
    </li>
    </$list>
    """).strip()

    # write all helper tiddlers 
    (tiddlers_dir / "__site-title.tid").write_text(site_title, encoding="utf-8")
    (tiddlers_dir / "__site-subtitle.tid").write_text(site_subtitle, encoding="utf-8")
    (tiddlers_dir / "__lang-state.tid").write_text(lang_state, encoding="utf-8")
    (tiddlers_dir / "__lang-switcher.tid").write_text(lang_switcher, encoding="utf-8")
    (tiddlers_dir / "__lang-macros.tid").write_text(lang_macros, encoding="utf-8")
    (tiddlers_dir / "__tag-label-macro.tid").write_text(tag_label_macro, encoding="utf-8")
    (tiddlers_dir / "__list-item.tid").write_text(list_item, encoding="utf-8")
    (tiddlers_dir / "__title-default.tid").write_text(title_default, encoding="utf-8")
    (tiddlers_dir / "__recent-sidebar.tid").write_text(recent_sidebar, encoding="utf-8")
    (tiddlers_dir / "__tag-template.tid").write_text(tag_template, encoding="utf-8")
    (tiddlers_dir / "__tagmanager-listitem.tid").write_text(tagmanager_listitem, encoding="utf-8")
    (tiddlers_dir / "__tag-styles.tid").write_text(tag_styles, encoding="utf-8")
    (tiddlers_dir / "__tag-clickoutside-startup.tid").write_text(tag_clickoutside_startup, encoding="utf-8")

    # welcome + default-tiddlers
    (tiddlers_dir / "__welcome.tid").write_text(welcome_tiddler, encoding="utf-8")
    (tiddlers_dir / "__default-tiddlers.tid").write_text(default_tiddlers, encoding="utf-8")


# Creates the wiki by invoking TiddlyWiki CLI
def build_wiki():
    print("[publisher] Building wiki...", flush=True)
    ensure_tw_project()
    inject_tiddlers()

    # Build index of titles for autolinking
    en_titles, zh_titles = build_title_index()

    # Create the tiddlers
    created = create_tiddlers(en_titles, zh_titles)
    if created == 0:
        print("[publisher] No summaries found; nothing to publish.", flush=True)
        return

    create_tag_tiddlers()

    outdir = WIKI_WORKDIR / "output"
    outdir.mkdir(parents=True, exist_ok=True)

    # Build the wiki
    cmd = [
        "tiddlywiki", str(WIKI_WORKDIR),
        "--build", "index"
    ]

    print(f"[publisher] Running: {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)

    # Copy to SITE_DIR
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-r", str(outdir) + "/", str(SITE_DIR)], check=True)
    print(f"[publisher] Copied wiki to {SITE_DIR}/output", flush=True)

def main():
    print(f"[publisher] SUMMARY_DIR={SUMMARY_DIR} SITE_DIR={SITE_DIR}", flush=True)
    build_wiki()
    generate_summaries_output()
    create_homepage()
    inject_search_handler()
    print("[publisher] Done.", flush=True)

if __name__ == "__main__":
    main()