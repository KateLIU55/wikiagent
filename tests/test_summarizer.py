"""
==============================================================
Summarizer Unit Tests
==============================================================

HOW TO RUN THE TESTS
--------------------

These tests can be executed using pytest. On Windows, the
recommended way is to invoke Python through the `py` launcher.

Basic test run:
    py -m pytest

Show each test with PASS/FAIL (recommended):
    py -m pytest -vv

Show verbose output + print() statements:
    py -m pytest -vv -s

Run only tests in this file:
    py -m pytest tests/test_summarizer.py -vv

If pytest is not installed, install it with:
    py -m pip install pytest

These tests are fully isolated and use tmp_path + monkeypatch
to avoid touching real project directories. All external calls
to the OpenAI client are mocked so no LLM is required.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import the summarizer functions
from summarizer.app import (
    strip_wikilinks_markup,
    strip_chinese_notes,
    process_once,
    summarize_en,
    chat_once,
)


# --------------------------------------------------------
# Helper function: writes a clean JSON file into the mocked
# clean directory (tmp_path) to simulate crawler output.
# Each test uses this to create input data for process_once().
# --------------------------------------------------------
def write_clean_file(tmp_path, name, data):
    p = tmp_path / "clean" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# --------------------------------------------------------
# TEST 1:
# strip_wikilinks_markup should correctly remove wiki-style
# [[link]] and [[link|label]] markup while preserving the
# visible text.
#
# EXPECTATION:
#   - [[Nanjing]] → Nanjing
#   - [[Yangtze River|the river]] → the river
# --------------------------------------------------------
def test_strip_wikilinks_markup_basic():
    text = "This is [[Nanjing]] and [[Yangtze River|the river]]."
    cleaned = strip_wikilinks_markup(text)
    assert cleaned == "This is Nanjing and the river."


# --------------------------------------------------------
# TEST 2:
# strip_chinese_notes should remove Chinese "note" lines
# beginning with "注：" (common in raw scraped content).
#
# EXPECTATION:
#   - Remove the note line entirely
#   - Preserve the rest of the text, including newlines
# --------------------------------------------------------
def test_strip_chinese_notes():
    text = "南京是城市。\n注：这是一个测试行。\n这是正文。"
    cleaned = strip_chinese_notes(text)
    assert cleaned == "南京是城市。\n这是正文。"


# --------------------------------------------------------
# TEST 3:
# process_once should SKIP generating summaries when all
# content (English & Chinese) is below MIN_INPUT_CHARS.
#
# HOW:
#   - Monkeypatch CLEAN_DIR, SUMMARY_DIR, DATA_DIR to use
#     a temporary directory so tests do not affect real data.
#   - Provide a JSON file with very short content.
#
# EXPECTATION:
#   - process_once() returns 0 (no summaries written)
#   - No output file is created in summarized/
# --------------------------------------------------------
@patch("summarizer.app.client")
def test_process_once_skips_short_content(mock_client, tmp_path, monkeypatch):

    monkeypatch.setattr("summarizer.app.DATA_DIR", tmp_path)
    monkeypatch.setattr("summarizer.app.CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr("summarizer.app.SUMMARY_DIR", tmp_path / "summarized")

    write_clean_file(tmp_path, "short_article", {
        "url": "https://example.com",
        "doc_type": "article",
        "lang": "en",
        "content": "Too short.",
        "categories": []
    })

    wrote = process_once()
    assert wrote == 0


# --------------------------------------------------------
# TEST 4:
# summarize_en should call the LLM once and return the
# model's generated text.
#
# HOW:
#   - Patch client.chat.completions.create to prevent any
#     real LLM calls.
#   - Provide fake return value "Fake summary".
#
# EXPECTATION:
#   - summarize_en returns exactly "Fake summary"
# --------------------------------------------------------
@patch("summarizer.app.client.chat.completions.create")
def test_summarize_en_calls_llm(mock_llm):
    mock_llm.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="Fake summary"))]
    )
    out = summarize_en("Long enough content for summarization.")
    assert out == "Fake summary"


# --------------------------------------------------------
# TEST 5:
# Full integration-style test of process_once().
#
# This verifies:
#   - The summarizer detects long EN content
#   - The LLM is called (mocked)
#   - A JSON summary file is created under summarized/
#   - summary_en field is correctly populated
#
# HOW:
#   - Mock global directories to use tmp_path
#   - Mock LLM call to avoid actual API usage
#   - Write a >280-character English article into clean/
#
# EXPECTATION:
#   - process_once returns 1 (one summary written)
#   - summarized/big_article.json is created
#   - JSON contains summary_en = "Fake summary"
# --------------------------------------------------------
@patch("summarizer.app.client.chat.completions.create")
def test_process_once_generates_summary(mock_llm, tmp_path, monkeypatch):
    mock_llm.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="Fake summary"))]
    )

    monkeypatch.setattr("summarizer.app.DATA_DIR", tmp_path)
    monkeypatch.setattr("summarizer.app.CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr("summarizer.app.SUMMARY_DIR", tmp_path / "summarized")

    sample_text = "Nanjing is a large city with history. " * 20

    write_clean_file(tmp_path, "big_article", {
        "url": "https://example.com",
        "doc_type": "article",
        "lang": "en",
        "content": sample_text,
        "categories": []
    })

    wrote = process_once()
    assert wrote == 1

    out_file = tmp_path / "summarized" / "big_article.json"
    assert out_file.exists()

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["summary_en"] == "Fake summary"
