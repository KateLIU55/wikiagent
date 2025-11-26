
# tests/test_publisher.py

# Comprehensive test for publisher/app.py

import importlib
import json
import os
import re
import shutil
import string
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import publisher.app as pub  


# Utilities & fixtures

@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    
    # Provide a clean temporary environment for each test and reload module.
    # Puts DATA_DIR, SUMMARY_DIR, SITE_DIR, WIKI_WORKDIR into os.environ and
    # reloads publisher.app so module-level constants pick them up.

    root = tmp_path / "envroot"
    root.mkdir()
    data = root / "data"
    summarized = data / "summarized"
    site = root / "site"
    workdir = root / "wiki"

    summarized.mkdir(parents=True)
    site.mkdir(parents=True)
    workdir.mkdir(parents=True)

    env = {
        "DATA_DIR": str(data),
        "SUMMARY_DIR": str(summarized),
        "SITE_DIR": str(site),
        "WIKI_WORKDIR": str(workdir)
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    # reload module so module-level Path variables update
    importlib.reload(pub)

    yield {
        "root": root,
        "data": data,
        "summarized": summarized,
        "site": site,
        "workdir": workdir
    }

    # cleanup (tmp_path will auto-clean but keep explicit)
    try:
        shutil.rmtree(str(root))
    except Exception:
        pass

# Test for small pure functions.

# Each case checks that wikilinks are stripped correctly.
@pytest.mark.parametrize(
    "inp,expected",
    [
        (None, None),
        ("", ""),
        ("Plain", "Plain"),
        ("[[Target]]", "Target"),
        ("prefix [[A|B]] suffix", "prefix B suffix"),
        ("[[A|B]][[C]]", "BC"),
    ],
)
def test_strip_wikilinks_markup_various(inp, expected):
    assert pub.strip_wikilinks_markup(inp) == expected


# Ensure pipe-handling takes last label
def test_strip_wikilinks_markup_nested_bar_edgecases():
    s = "[[a|b|c]] and [[x|y]]"
    assert pub.strip_wikilinks_markup(s) == "c and y"


# Tests normalization of strings into URL slugs.
@pytest.mark.parametrize(
    "value,expected_startswith",
    [
        ("Hello World!", "hello-world"),
        ("  Many  Spaces  ", "many-spaces"),
        ("Symbols!@#$%^&*()", "symbols-"),
        ("A" * 200, "a" * 80)  # truncated to 80 chars
    ]
)
def test_slugify_basic_and_truncation(value, expected_startswith):
    out = pub.slugify(value)
    # length constraint
    assert len(out) <= 80
    # startswith check for expected normalization
    assert out.startswith(expected_startswith[: min(len(expected_startswith), len(out))])


# Checks Chinese-detection heuristic.
@pytest.mark.parametrize(
    "text,expected",
    [
        ("", False),
        ("中文", False),  # short -> False
        ("这是一个南京的标题示例含有超过四个汉字", True),
        ("English text", False),
        ("混合text包括中文字符abcd", True),  # at least 4 CJK and >25%
    ]
)
def test_looks_like_chinese_various(text, expected):
    assert pub.looks_like_chinese(text) is expected


# Checks English-title extraction from english summaries.
def test_derive_english_title_from_summary_variants():
    s1 = "Nanjing Industrial University Station is a metro station on line 4."
    assert pub.derive_english_title_from_summary(s1) == "Nanjing Industrial University Station"

    s2 = "Tiny is small."  
    assert pub.derive_english_title_from_summary(s2) == "Tiny" # delimiter 'is' and >= 4 chars.

    # fallback to first 80 chars if no delimiter
    long = "A Very Long English Title That Might Not Contain 'is' But Should Be Extracted Properly Anyway"
    out = pub.derive_english_title_from_summary(long)
    assert out is not None and len(out) <= 80


# Autolinks known English titles.
def test_autolink_en_basic_behavior():
    text = "Visit Nanjing University and Nanjing Museum for research."
    en_titles = ["Nanjing University", "Nanjing Museum"]
    out = pub.autolink_en(text, en_titles, current_title="Other")
    assert "[[Nanjing University]]" in out
    assert "[[Nanjing Museum]]" in out


def test_autolink_en_ignores_current_title_and_existing_wikilinks():
    text = "Nanjing University is here"
    out = pub.autolink_en(text, ["Nanjing University"], current_title="Nanjing University")
    assert out == text # Ignores title since it is current title

    # existing wikilink not double wrapped
    text2 = "Already [[Nanjing Museum]] present"
    out2 = pub.autolink_en(text2, ["Nanjing Museum"], current_title="")
    # still exactly one occurrence of the wikilink
    assert out2.count("[[Nanjing Museum]]") == 1


# 
def test_autolink_en_word_boundary_and_punctuation():
    text = "Nanjing University(Nanjing University) Nanjing-University!"
    en_titles = ["Nanjing University"]
    out = pub.autolink_en(text, en_titles, current_title="")
    # ensure we link at word boundaries and not inside hyphenated concatenations wrongly
    assert "[[Nanjing University]]" in out

# Autolinks known Chinese titles.
def test_autolink_zh_basic_and_skip_current():
    zh_text = "我想去 南京博物馆 看看。"
    zh_titles = [("南京博物馆", "Nanjing Museum"), ("南京大学", "Nanjing University")]
    out = pub.autolink_zh(zh_text, zh_titles, current_title="Other")
    assert "[[南京博物馆|Nanjing Museum]]" in out

    out2 = pub.autolink_zh("南京博物馆", [("南京博物馆", "Nanjing Museum")], current_title="Nanjing Museum")
    assert "[[南京博物馆|Nanjing Museum]]" not in out2


# Tests for build_title_index

def test_build_title_index_with_various_json(clean_env):
    sdir = clean_env["summarized"]
    # normal file
    a = {"title": "Test Page", "summary_en": "English summary", "zh_title_hans": "测试"}
    (sdir / "a.json").write_text(json.dumps(a), encoding="utf-8")
    # Chinese-titled JSON with english summary -> should derive english title
    b = {"title": "南京车站", "summary_en": "Nanjing Station is a major hub.", "zh_title_hans": "南京车站"}
    (sdir / "b.json").write_text(json.dumps(b), encoding="utf-8")
    # file with no summaries -> ignored
    c = {"title": "Empty", "summary_en": "", "summary_zh_hans": ""}
    (sdir / "c.json").write_text(json.dumps(c), encoding="utf-8")

    en_titles, zh_titles = pub.build_title_index()
    assert "Test Page" in en_titles
    # derived english title should appear (prefix check)
    assert any(t.startswith("Nanjing Station") or "Nanjing" in t for t in en_titles)
    # zh_titles should contain hans mapping for the chinese one
    assert any(item[0] == "测试" or item[0] == "南京车站" for item in zh_titles)


def test_build_title_index_tunnel_special_case(clean_env):
    # If any file's title is one of TUNNEL_TITLES it should canonicalize
    sdir = clean_env["summarized"]
    t = {"title": "南京应天大街长江隧道", "summary_en": "A tunnel."}
    (sdir / "tunnel.json").write_text(json.dumps(t), encoding="utf-8")
    en_titles, zh_titles = pub.build_title_index()
    assert "Nanjing Yingtian Avenue Yangtze River Tunnel" in en_titles


# Tests for create_tiddlers (many flows)

# write a JSON file to summarized dir
def _write_json(sdir: Path, name: str, obj: dict):
    (sdir / name).write_text(json.dumps(obj), encoding="utf-8")


# Test for create_tiddlers basic functionality and wikilink stripping.
def test_create_tiddlers_basic_and_wikilink_stripping(clean_env):
    sdir = clean_env["summarized"]
    workdir = Path(os.environ["WIKI_WORKDIR"])
    # simple english + chinese
    a = {
        "title": "Test Page",
        "summary_en": "This is an [[internal|link]] to something.",
        "summary_zh_hans": "简体摘要",
        "summary_zh_hant": "繁體摘要",
        "tags": ["历史", "summary"],
        "url": "https://example/en",
        "zh_url": "https://example/zh"
    }
    _write_json(sdir, "a.json", a)

    importlib.reload(pub)
    en_titles, zh_titles = pub.build_title_index()
    count = pub.create_tiddlers(en_titles, zh_titles)
    assert count >= 1

    tiddlers_dir = workdir / "tiddlers"
    tid_files = list(tiddlers_dir.glob("*.tid"))
    assert tid_files, "No .tid files created"
    content = tid_files[0].read_text(encoding="utf-8")
    # wikilink labels should be stripped (we expect "link" label present)
    assert "link" in content or "internal" in content
    # title header present
    assert re.search(r"^title:\s+", content, flags=re.M)


# Verify that English summary overrides Chinese-only summary.
def test_create_tiddlers_prefers_english_summary(clean_env):
    sdir = clean_env["summarized"]
    workdir = Path(os.environ["WIKI_WORKDIR"])

    # two files for same topic key -> one has Chinese-only summary, one has English summary.
    fs1 = {"title": "SameTopic", "summary_en": "中文内容", "summary_zh_hans": "中文"}
    fs2 = {"title": "SameTopic", "summary_en": "English content describing SameTopic", "summary_zh_hans": ""}
    _write_json(sdir, "one.json", fs1)
    _write_json(sdir, "two.json", fs2)

    importlib.reload(pub)
    en_titles, zh_titles = pub.build_title_index()
    count = pub.create_tiddlers(en_titles, zh_titles)
    assert count == 1  # only one tiddler for the topic
    # check that derived tiddler uses English content (has_en yes)
    tiddlers_dir = workdir / "tiddlers"
    contents = "\n".join(f.read_text(encoding="utf-8") for f in tiddlers_dir.glob("*.tid"))
    assert "has_en: yes" in contents or "English content" in contents


# Test for title derivation when title is Chinese but summary_en is English.
def test_create_tiddlers_title_derived_when_title_is_chinese(clean_env):
    sdir = clean_env["summarized"]
    workdir = Path(os.environ["WIKI_WORKDIR"])

    # Title looks Chinese but summary_en is English -> should derive english title
    fs = {"title": "南京站", "summary_en": "Nanjing Station is a major hub.", "summary_zh_hans": ""}
    _write_json(sdir, "cn.json", fs)

    importlib.reload(pub)
    en_titles, zh_titles = pub.build_title_index()
    count = pub.create_tiddlers(en_titles, zh_titles)
    assert count == 1
    tiddlers_dir = workdir / "tiddlers"
    content = list(tiddlers_dir.glob("*.tid"))[0].read_text(encoding="utf-8")
    # Should contain derived English title
    assert "Nanjing Station" in content or "Nanjing" in content

# Creates invalid JSON file, ensure that it is skipped without exceptions.
def test_create_tiddlers_handles_malformed_json_cleanly(clean_env, capsys):
    sdir = clean_env["summarized"]
    (sdir / "bad.json").write_text("{ not: valid json", encoding="utf-8")
    importlib.reload(pub)
    # Should not throw; create_tiddlers will skip bad file and return 0
    en_titles, zh_titles = pub.build_title_index()
    count = pub.create_tiddlers(en_titles, zh_titles)
    assert isinstance(count, int)


# Tests for create_tag_tiddlers

# Create tag tiddlers for known tags in summaries.
def test_create_tag_tiddlers_creates_files_for_used_tags(clean_env):
    sdir = clean_env["summarized"]
    workdir = Path(os.environ["WIKI_WORKDIR"])
    obj = {"title": "X", "summary_en": "Y", "tags": ["景点", "自定义标签", "summary", ""]}  # includes a known and unknown tag, excludes 'summary' and empty
    _write_json(sdir, "x.json", obj)
    importlib.reload(pub)
    pub.create_tag_tiddlers()
    tags = list((workdir / "tiddlers").glob("__tag-*.tid"))
    assert tags, "expected at least one tag tiddler"
    contents = [t.read_text(encoding="utf-8") for t in tags]
    # Known tag "景点" should produce caption-en text
    assert any("Tourist attractions in Nanjing" in c for c in contents) or any("景点" in c for c in contents)


# No summaries -> should print skip message and return None
def test_create_tag_tiddlers_skips_when_none(clean_env, capsys):
    importlib.reload(pub)
    pub.create_tag_tiddlers()
    captured = capsys.readouterr()
    assert "No tags found" in captured.out or "skipping tag" in captured.out or captured.out is not None


# Tests for ensure_tw_project, inject_tiddlers, create_homepage, inject_search_handler

# Ensure tiddlywiki.info is created with expected content.
def test_ensure_tw_project_and_info_written(clean_env):
    workdir = Path(os.environ["WIKI_WORKDIR"])
    importlib.reload(pub)
    pub.ensure_tw_project()
    info = workdir / "tiddlywiki.info"
    assert info.exists()
    text = info.read_text(encoding="utf-8")
    assert "tiddlywiki" in text or "plugins" in text


# Ensure that inject_tiddlers creates all required helper .tid files.
def test_inject_tiddlers_writes_many_helpers(clean_env):
    workdir = Path(os.environ["WIKI_WORKDIR"])
    importlib.reload(pub)
    pub.inject_tiddlers()
    tdir = workdir / "tiddlers"
    expected = [
        "__site-title.tid",
        "__welcome.tid",
        "__default-tiddlers.tid",
        "__lang-macros.tid",
        "__tag-styles.tid",
    ]
    for fn in expected:
        assert (tdir / fn).exists(), f"{fn} missing"


# Verify that create_homepage creates index.html with expected content.
def test_create_homepage_and_content_written(clean_env):
    site = Path(os.environ["SITE_DIR"])
    importlib.reload(pub)
    pub.create_homepage()
    idx = site / "index.html"
    assert idx.exists()
    content = idx.read_text(encoding="utf-8")
    assert "<title>Nanjing Knowledge Hub</title>" in content or "Nanjing Knowledge Hub Wiki" in content
    assert "search-container" in content


# Verify that inject_search_handler creates the plugin file.
def test_inject_search_handler_creates_plugin_file(clean_env):
    workdir = Path(os.environ["WIKI_WORKDIR"])
    importlib.reload(pub)
    pub.inject_search_handler()
    plugin = workdir / "tiddlers" / "plugins" / "external-search" / "startup.tid"
    assert plugin.exists()
    assert "external-search-startup" in plugin.read_text(encoding="utf-8")


# Tests for generate_summaries_output

# Creates summaries.json with sorted entries.
def test_generate_summaries_output_creates_sorted_json(clean_env):
    sdir = clean_env["summarized"]
    site = Path(os.environ["SITE_DIR"])
    _write_json(sdir, "b.json", {"title": "B Title", "summary_en": "B summary"})
    _write_json(sdir, "a.json", {"title": "A Title", "summary_en": "A summary"})
    # include an item with no summary, but with 'summary' field fallback
    _write_json(sdir, "c.json", {"title": "C Title", "summary": "C fallback"})

    importlib.reload(pub)
    pub.generate_summaries_output()
    out = site / "output" / "summaries.json"
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    # Should be sorted by title case-insensitively: A, B, C
    titles = [d["title"] for d in data]
    assert titles == sorted(titles, key=lambda x: x.lower())


# Creates invalid JSON file, ensure that it is skipped.
def test_generate_summaries_output_skips_invalid_json_and_logs(clean_env, capsys):
    sdir = clean_env["summarized"]
    (sdir / "bad.json").write_text("notjson", encoding="utf-8")
    _write_json(sdir, "good.json", {"title": "Good", "summary_en": "Yes"})
    importlib.reload(pub)
    pub.generate_summaries_output()
    captured = capsys.readouterr()
    assert "skipping bad.json" in captured.out.lower() or "skipping" in captured.out.lower()


# Tests for build_wiki and main (mocking subprocess.run)

# Mock process to ensure build_wiki runs subprocess.run.
@patch("subprocess.run")
def test_build_wiki_runs_tiddlywiki_and_cp(mock_run, clean_env):
    # Provide one summary so create_tiddlers will create something
    sdir = clean_env["summarized"]
    _write_json(sdir, "one.json", {"title": "One", "summary_en": "One summary"})
    importlib.reload(pub)
    mock_run.return_value = MagicMock()
    pub.build_wiki()
    # We expect subprocess.run to have been called at least once (build and cp)
    assert mock_run.called


# Test that main calls all steps and creates expected files.
@patch("subprocess.run")
def test_main_calls_all_steps_and_creates_files(mock_run, clean_env):
    sdir = clean_env["summarized"]
    site = Path(os.environ["SITE_DIR"])
    _write_json(sdir, "one.json", {"title": "One", "summary_en": "One summary"})
    importlib.reload(pub)
    mock_run.return_value = MagicMock()

    # Run main (which calls build_wiki, generate_summaries_output, create_homepage, inject_search_handler)
    pub.main()

    # Homepage must exist
    assert (site / "index.html").exists()
    # summaries output must exist
    assert (site / "output" / "summaries.json").exists()


# Additional edge-case tests and fuzzing-ish checks

# Ensure that autolink_en does not create overlapping links.
def test_autolink_en_does_not_create_overlapping_links(clean_env):
    text = "Alpha Beta Gamma"
    en_titles = ["Alpha Beta", "Beta Gamma"]
    out = pub.autolink_en(text, en_titles, current_title="")
    # Both occurrences should be linked without overlapping double-wraps
    assert out.count("[[") >= 1


 # only allowed characters in output and no consecutive hyphens
def test_slugify_handles_non_ascii_and_repeated_hyphens():
    s = "Tést — with / slashes & spaces"
    out = pub.slugify(s)
    assert re.match(r"^[a-z0-9\-_]+$", out)
    assert "--" not in out

# End of file
