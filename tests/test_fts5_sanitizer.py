"""Tests for _sanitize_fts5_query — FTS5 query safety and CJK handling."""

from trade_compass_agent.memory.tree.search import _sanitize_fts5_query


class TestSanitizeFts5Query:
    def test_empty_input(self):
        assert _sanitize_fts5_query("") == ""
        assert _sanitize_fts5_query("   ") == ""

    def test_pure_punctuation(self):
        assert _sanitize_fts5_query("...") == ""
        assert _sanitize_fts5_query("!!!???") == ""
        assert _sanitize_fts5_query("- - -") == ""

    def test_simple_tokens(self):
        result = _sanitize_fts5_query("ETF 基金")
        assert '"ETF"' in result
        assert '"基金"' in result

    def test_dots_in_token(self):
        result = _sanitize_fts5_query("涨跌幅.比例")
        assert result  # should not be empty
        # should not raise FTS5 syntax error when used

    def test_embedded_double_quotes(self):
        result = _sanitize_fts5_query('he said "hello"')
        assert '""hello""' in result

    def test_fts5_operators_stripped(self):
        result = _sanitize_fts5_query("ETF AND OR NOT 基金")
        assert "AND" not in result.replace('"AND"', "")
        assert "OR" not in result.replace('"OR"', "").replace(" OR ", "")
        assert "NOT" not in result.replace('"NOT"', "")
        assert '"ETF"' in result
        assert '"基金"' in result

    def test_near_operator_stripped(self):
        result = _sanitize_fts5_query("NEAR 涨停")
        assert "NEAR" not in result.replace('"NEAR"', "")

    def test_cjk_bigram_split(self):
        result = _sanitize_fts5_query("龙头股异动")
        # 4 chars → 3 bigrams: 龙头, 头股, 股异, (异动 is handled by the bigram logic)
        assert '"龙头"' in result
        assert '"头股"' in result
        assert '"股异"' in result

    def test_cjk_short_no_split(self):
        result = _sanitize_fts5_query("龙头")
        assert result == '"龙头"'

    def test_mixed_cjk_and_ascii(self):
        result = _sanitize_fts5_query("ETF 龙头股异动 fund")
        assert '"ETF"' in result
        assert '"fund"' in result
        assert '"龙头"' in result

    def test_result_uses_or_join(self):
        result = _sanitize_fts5_query("hello world")
        assert " OR " in result

    def test_single_token(self):
        result = _sanitize_fts5_query("ETF")
        assert result == '"ETF"'
