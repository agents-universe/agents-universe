"""Frontmatter locale-map normalization.

Definition fields accept a shared scalar (every existing file) or a
``{locale: value}`` map. Normalization must collapse both to a canonical
string for legacy/LLM-facing consumers while keeping the full map for UI
serialization — and must never leak ``str(dict)``.
"""
from __future__ import annotations

from agent_core.localization import (
    is_locale_map,
    normalize_i18n_field,
    normalize_i18n_prompts,
)


class TestIsLocaleMap:
    def test_scalar_is_not_map(self):
        assert is_locale_map("text") is False
        assert is_locale_map(None) is False
        assert is_locale_map(["zh-CN"]) is False

    def test_empty_dict_is_not_map(self):
        assert is_locale_map({}) is False

    def test_locale_keys(self):
        assert is_locale_map({"zh-CN": "a", "en-US": "b"}) is True
        assert is_locale_map({"zh": "a"}) is True

    def test_non_locale_keys(self):
        # A skill ref ({slug, description}) must never count as a map.
        assert is_locale_map({"slug": "x", "description": "y"}) is False
        assert is_locale_map({"zh_CN": "a"}) is False
        assert is_locale_map({"zh-CN": "a", "bad key": "b"}) is False


class TestNormalizeI18nField:
    def test_scalar_passthrough(self):
        assert normalize_i18n_field("hello") == ("hello", None)

    def test_none_and_numbers_yield_none(self):
        assert normalize_i18n_field(None) == (None, None)
        assert normalize_i18n_field(42) == (None, None)

    def test_zh_first_canonical_order(self):
        value = {"en-US": "Tech Lead", "zh-CN": "技术负责人"}
        canonical, locale_map = normalize_i18n_field(value)
        assert canonical == "技术负责人"
        assert locale_map == {"en-US": "Tech Lead", "zh-CN": "技术负责人"}

    def test_en_first_canonical_order(self):
        value = {"en-US": "Tech Lead", "zh-CN": "技术负责人"}
        canonical, _ = normalize_i18n_field(value, canonical_order=("en-US", "zh-CN"))
        assert canonical == "Tech Lead"

    def test_missing_preferred_locale_falls_back_to_first_entry(self):
        value = {"en-US": "Only English"}
        canonical, locale_map = normalize_i18n_field(value)  # zh-first default
        assert canonical == "Only English"
        assert locale_map == {"en-US": "Only English"}

    def test_non_locale_order_entry_skipped(self):
        value = {"fr": "Seul", "en-US": "Only English"}
        canonical, _ = normalize_i18n_field(value, canonical_order=("zh-CN", "en-US"))
        assert canonical == "Only English"

    def test_non_string_values_dropped(self):
        value = {"zh-CN": ["a", "b"], "en-US": "text"}
        canonical, locale_map = normalize_i18n_field(value)
        assert canonical == "text"
        assert locale_map == {"en-US": "text"}

    def test_map_with_no_string_values_is_none(self):
        assert normalize_i18n_field({"zh-CN": ["a"], "en-US": ["b"]}) == (None, None)

    def test_skill_ref_dict_is_not_a_map(self):
        assert normalize_i18n_field({"slug": "x", "description": "y"}) == (None, None)


class TestNormalizeI18nPrompts:
    def test_bare_string_is_one_prompt_never_comma_split(self):
        # Prompts legitimately contain commas — "a, b" is ONE prompt.
        assert normalize_i18n_prompts("fix login, then deploy") == (
            ["fix login, then deploy"],
            None,
        )

    def test_shared_list(self):
        assert normalize_i18n_prompts(["a", " ", "b", 3]) == (["a", "b"], None)

    def test_locale_map_of_lists(self):
        value = {
            "zh-CN": ["评审这个 PR", "解决阻塞"],
            "en-US": ["Review this PR", "Resolve the blocker"],
        }
        canonical, locale_map = normalize_i18n_prompts(value)
        assert canonical == ["评审这个 PR", "解决阻塞"]
        assert locale_map == value

    def test_locale_map_en_first(self):
        value = {"zh-CN": ["中"], "en-US": ["en"]}
        canonical, _ = normalize_i18n_prompts(value, canonical_order=("en-US", "zh-CN"))
        assert canonical == ["en"]

    def test_locale_map_single_string_values(self):
        value = {"zh-CN": "一个问题", "en-US": "One question"}
        canonical, locale_map = normalize_i18n_prompts(value)
        assert canonical == ["一个问题"]
        assert locale_map == {"zh-CN": ["一个问题"], "en-US": ["One question"]}

    def test_locale_map_blanks_dropped_per_language(self):
        value = {"zh-CN": [" ", "保留"], "en-US": []}
        canonical, locale_map = normalize_i18n_prompts(value)
        assert canonical == ["保留"]
        assert locale_map == {"zh-CN": ["保留"]}

    def test_invalid_shapes(self):
        assert normalize_i18n_prompts(None) == ([], None)
        assert normalize_i18n_prompts(42) == ([], None)
        assert normalize_i18n_prompts({"zh-CN": [1], "en-US": [2]}) == ([], None)
