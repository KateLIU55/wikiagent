# tests/test_extractor.py

import os
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

# Make sure the project root is on sys.path so we can import extractor.app
ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

import extractor.app as extractor  # type: ignore


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# ---------- doc_type_from_url ----------

def test_doc_type_from_url_category_article_unknown():
    category_url = "https://en.wikipedia.org/wiki/Category:Nanjing"
    article_url = "https://en.wikipedia.org/wiki/Nanjing"
    empty_url = ""

    assert extractor.doc_type_from_url(category_url) == "category"
    assert extractor.doc_type_from_url(article_url) == "article"
    assert extractor.doc_type_from_url(empty_url) == "unknown"


# ---------- url_from_raw_html ----------

def test_url_from_raw_html_uses_canonical_link():
    html = b"""
    <html>
      <head>
        <link rel="canonical" href="https://en.wikipedia.org/wiki/Nanjing" />
      </head>
      <body>hello</body>
    </html>
    """
    url = extractor.url_from_raw_html(html)
    assert url == "https://en.wikipedia.org/wiki/Nanjing"


def test_url_from_raw_html_returns_none_if_missing():
    html = b"<html><head></head><body>No canonical</body></html>"
    assert extractor.url_from_raw_html(html) is None


# ---------- classify_doc ----------

def test_classify_doc_list_from_title():
    soup = make_soup("<html><body><p>hello</p></body></html>")
    doc_type = extractor.classify_doc("List of bridges in Nanjing", "https://en.wikipedia.org/wiki/Foo", soup)
    assert doc_type == "list"


def test_classify_doc_disambiguation_via_css_class():
    html = """
    <html>
      <body>
        <div class="mw-disambig">This is a disambiguation page</div>
      </body>
    </html>
    """
    soup = make_soup(html)
    doc_type = extractor.classify_doc("Nanjing", "https://en.wikipedia.org/wiki/Nanjing", soup)
    assert doc_type == "disambiguation"


def test_classify_doc_disambiguation_via_category():
    html = """
    <html>
      <body>
        <div id="mw-normal-catlinks">
          <a>Nanjing</a>
          <a>Nanjing disambiguation pages</a>
        </div>
      </body>
    </html>
    """
    soup = make_soup(html)
    doc_type = extractor.classify_doc("Nanjing", "https://en.wikipedia.org/wiki/Nanjing", soup)
    assert doc_type == "disambiguation"


# ---------- extract_text / extract_text_from_soup ----------

def test_extract_text_strips_script_style_and_joins_paragraphs():
    html = """
    <html>
      <body>
        <script>console.log("x")</script>
        <style>body {color:red;}</style>
        <p>First paragraph.</p>
        <p>Second paragraph.</p>
        <noscript>nope</noscript>
      </body>
    </html>
    """
    text = extractor.extract_text(html.encode("utf-8"))

    # Scripts/styles/noscript removed, paragraphs joined with blank line
    assert "console.log" not in text
    assert "color:red" not in text
    assert "nope" not in text
    assert "First paragraph." in text
    assert "Second paragraph." in text
    assert "\n\n" in text  # joined with blank line


# ---------- find_interlanguage_links ----------

def test_find_interlanguage_links_prefers_hans_and_hant():
    html = """
    <html>
      <body>
        <div id="p-lang">
          <a hreflang="zh" href="https://zh.wikipedia.org/wiki/Nanjing_zh">中文</a>
          <a hreflang="zh-hans" href="https://zh.wikipedia.org/wiki/Nanjing_hans">简体</a>
          <a hreflang="zh-hant" href="https://zh.wikipedia.org/wiki/Nanjing_hant">繁體</a>
        </div>
      </body>
    </html>
    """
    soup = make_soup(html)
    links = extractor.find_interlanguage_links(soup)

    assert links["zh_hans"] == "https://zh.wikipedia.org/wiki/Nanjing_hans"
    assert links["zh_hant"] == "https://zh.wikipedia.org/wiki/Nanjing_hant"
    # generic zh still captured
    assert links["zh"] == "https://zh.wikipedia.org/wiki/Nanjing_zh"


# ---------- chinese_variants_from_en_html (with monkeypatch) ----------

def test_chinese_variants_from_en_html_uses_interlanguage_links(monkeypatch):
    # English page HTML with language links
    en_html = """
    <html>
      <body>
        <div id="p-lang">
          <a hreflang="zh-hans" href="https://zh.wikipedia.org/wiki/Nanjing_hans">简体</a>
          <a hreflang="zh-hant" href="https://zh.wikipedia.org/wiki/Nanjing_hant">繁體</a>
        </div>
      </body>
    </html>
    """.encode("utf-8")   # <-- convert to bytes here
    

    # Fake HTML returned for Chinese pages
    def fake_html_for_url(url: str) -> bytes | None:
        if "hant" in url:
            return b"<html><body><p>Traditional Chinese text</p></body></html>"
        else:
            return b"<html><body><h1>Chinese title</h1><p>Simplified Chinese text</p></body></html>"

    # Patch html_for_url so we don't hit the network / DB
    monkeypatch.setattr(extractor, "html_for_url", fake_html_for_url)

    zh_url, hans_title, hans_text, hant_text = extractor.chinese_variants_from_en_html(en_html)

    assert zh_url == "https://zh.wikipedia.org/wiki/Nanjing_hans"
    assert hans_title == "Chinese title"
    assert "Simplified Chinese text" in (hans_text or "")
    assert "Traditional Chinese text" in (hant_text or "")
