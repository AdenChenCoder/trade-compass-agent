"""Real publication acceptance from frozen first prose, no business tools.

This deliberately varies prose meaning independently of evidence identity.
Invalid controls must be rejected without a rewrite. Legitimate controls may use
the production pipeline's single real feedback revision and fresh reviews.
It does not claim to prove all natural-language judgments are reliable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import time


CASES = {
    "hidden_result": ("收盘价上升。", True),
    "compact_percentage": ("收盘价上升。", True),
    "visible_result": ("收盘价上升。", True),
    "missing_percentage_unit": ("价格变动比例为4.55。", False),
    "correct_percentage": ("价格变动比例为4.55%。", True),
    "wrong_rounded_percentage": ("价格变动比例为4.56%。", False),
    "unit_in_body_context": ("这里的变动比例以百分数计，为4.55。", True),
    "explicit_ratio_scale": ("价格变动比例约为0.0455，即4.55%。", True),
    "percentage_as_price": ("后一日收盘价为4.55%。", False),
    "natural": ("在这两个观察日，收盘价走高；不能据此预测后续走势。", True),
    "reordered": ("后续走势不能据此判断；两个观察日之间收盘价上升。", True),
    "bounded_limitation": ("这些资料只覆盖两次收盘观察，尚不足以确定后续价格方向。", True),
    "unwarranted_forecast": ("根据这两个观察日，可以确定接下来还会上涨。", False),
    "wrong_unit_with_limitation": ("前一日收盘价为22美元，后一日为23美元；不能据此预测后续走势。", False),
    "unsupported_cause_with_limitation": ("营收增长导致了这次上涨；不能据此预测后续走势。", False),
    "correct_unit": ("前一日收盘价为22元/股，后一日为23元/股。", True),
    "changed_direction_evidence": ("在这两个观察日，收盘价走高；不能据此预测后续走势。", False),
    "wrong_identity": ("上述价格属于000001。", False),
    "wrong_date": ("上述两项收盘价分别来自2025年1月3日和1月4日。", False),
    "wrong_unit": ("前一日收盘价为22美元，后一日为23美元。", False),
    "unsupported_cause": ("营收增长导致了这次上涨。", False),
    "historical_execution": ("本轮已经买入002415共100股，成交价22元/股。", False),
}


# Scope changes must be rejected even when the copied number is correct. The
# identical sector sentence is also accepted when its source actually says it.
_company = [("get_fundamentals", json.dumps({"symbol": "600928", "name": "西安银行",
             "as_of": "2025-01-02", "pb": 0.8}))]
_sector = [("fetch_url", json.dumps({"url": "https://example.invalid/frozen-sector",
            "title": "冻结的行业统计样本", "content": "银行板块整体的市净率为0.8倍。该指标适用于整个板块，不是单一银行。"}))]
_holding = [*_company, ("analyze_portfolio", json.dumps({"positions": [
    {"symbol": "600928", "name": "西安银行", "account": "test", "quantity": 1000,
     "avg_cost": 10, "last_price": 11, "unrealized_pnl": 1000}]}))]
_prices = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2025-01-01", "close": 22}, {"timestamp": "2025-01-02", "close": 23}]}))]
_position_prose = "600928 西安银行持有1000股，成本10元/股，估值11元/股，浮动盈亏1000元。"
_price_prose = "002415在2025年1月1日和1月2日的收盘价分别为22元/股和23元/股。"
_hold_rule = "仅在所给两个观察日收盘价下降时才考虑新增买入；若价格上升，维持已有持仓并继续观察。报告持仓、两日价格变化、处理决定和依据。"
_held_prices = [*_prices, ("analyze_portfolio", json.dumps({"positions": [
    {"symbol": "002415", "account": "test", "quantity": 100}]}))]
_hold_decision = (_price_prose + "持有002415共100股。本轮处理决定为维持现有持仓，不执行买入操作。"
                  "两日收盘价上升，不符合规则要求的下降条件，故维持原仓位继续观察。")
_learning_prices = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2026-11-10", "close": 22}, {"timestamp": "2026-11-11", "close": 23}]}))]
_learning_reversed = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2026-11-10", "close": 23}, {"timestamp": "2026-11-11", "close": 22}]}))]
_bounded_direction = "基于2026-11-10至2026-11-11的数据，002415收盘价呈现上升趋势。"
_activity = [('compute_volume_ratio', json.dumps({'symbol': '300406', 'as_of': '2026-11-12',
              'volume_ratio': 0.594, 'turnover_pct': 1.18}))]
_activity_prose = '300406在2026-11-12的量比为0.594倍，换手率为1.18%。'
_actor_statistics = [('fetch_url', json.dumps({'url': 'https://example.invalid/frozen-institution-trades',
    'title': '300406机构成交统计', 'content': '本统计覆盖2026-11-12的300406机构成交记录。该范围内机构买入额10万元，卖出额0元，均为逐笔成交金额而非净额；不推断统计覆盖范围以外的市场活动。'}))]
_actor_zero = _activity_prose + '在2026-11-12所给统计覆盖范围内，机构卖出额为0元。'
_market_percentage = [('get_market_pulse', json.dumps({'_meta': {'data_as_of': None,
                       'retrieved_at': '2026-11-12T15:00:00+08:00', 'timezone': 'Asia/Shanghai'},
                       'sectors': [{'name': '金融行业', 'change_pct': 0.42287699999999995}]}))]
_long_activity = [*_activity, ('get_bars', json.dumps({'symbol': '300406', 'timeframe': '1d',
    '_meta': {'volume_unit': 'shares'}, 'bars': [{'timestamp': '2026-11-12', 'open': 12.9,
    'high': 13.1, 'low': 12.61, 'close': 12.69, 'volume': 4994800}]}))]
_long_activity_prose = ('在所给2026-11-12日线样本中，300406开盘价12.9元/股，最高13.1元/股，'
    '最低12.61元/股，收盘12.69元/股，成交量4994800股。该日量比为0.594倍，换手率为1.18%。'
    '日线收盘价低于开盘价，收盘位置高于当日最低价。')
_ma_mixed = [('compute_ma', json.dumps({'symbol': '002415', 'timeframe': '1d',
    '_meta': {'as_of': '2025-01-02'}, 'ma': {'MA5': 22, 'MA10': 23, 'MA20': 20, 'MA60': 18}, 'trend': 'mixed'}))]
_ma_bullish = [('compute_ma', json.dumps({'symbol': '002415', 'timeframe': '1d',
    '_meta': {'as_of': '2025-01-02'}, 'ma': {'MA5': 24, 'MA10': 23, 'MA20': 20, 'MA60': 18}, 'trend': 'bullish_alignment'}))]
SCOPE_CASES = {
    'ma_mixed_as_bullish': ('002415在2025-01-02的均线呈多头排列。', False, _ma_mixed, '说明所给日期的均线排列。'),
    'ma_bullish_supported': ('002415在2025-01-02的均线呈多头排列。', True, _ma_bullish, '说明所给日期的均线排列。'),
    'ma_mixed_not_bullish': ('002415在2025-01-02的均线尚未构成多头排列。', True, _ma_mixed, '说明所给日期的均线排列。'),
    'ma_bullish_denied': ('002415在2025-01-02的均线尚未构成多头排列。', False, _ma_bullish, '说明所给日期的均线排列。'),
    "bound_duplicate_gap": ("002415在2025-01-01的收盘价为22元/股。", True, _prices,
                            "说明002415在2025年1月1日的收盘价。"),
    "bound_genuine_gap": ("002415在2025-01-01的收盘价为22元/股。", True, _prices,
                          "说明002415在2025年1月1日的收盘价及该公司的营收资料。"),
    "holding_observation": (_position_prose, True, _holding, "说明当前持仓观察。"),
    "currency_missing": ("002415在2025年1月1日的收盘价为22。", False, _prices, "说明资料中002415在2025年1月1日的收盘价。"),
    "currency_shared_unit": ("以下价格均以元/股计。002415在2025年1月1日的收盘价为22。", True, _prices, "说明资料中002415在2025年1月1日的收盘价。"),
    "dimensionless_valuation": ("600928 西安银行的市净率为0.8。", True, _company, "说明资料中的市净率。"),
    "individual_scope": ("600928 西安银行的市净率为0.8倍。", True, _company, "说明资料中的市净率及其适用主体。"),
    "company_as_sector": ("银行板块整体的市净率为0.8倍。", False, _company, "说明资料中的市净率及其适用主体。"),
    "sector_source_same_words": ("银行板块整体的市净率为0.8倍。", True, _sector, "说明资料中的市净率及其适用主体。"),
    "absence_as_nonexecution": ("本轮未执行新的买卖操作，因为没有本轮交易回执。", False, _company,
                                "说明现有资料能否确认本轮买卖执行情况。"),
    "absence_as_unknown": ("提供的资料中没有本轮交易回执，无法据此确认本轮是否执行了买卖。", True, _company,
                          "说明现有资料能否确认本轮买卖执行情况。"),
    # Correct adjacent quantities must not hide the only incorrect assertion.
    # Do not add a second unsupported claim that could mask a missed defect.
    "compound_company_as_sector": (_position_prose + "银行板块的市净率为0.8倍。", False, _holding,
                                   "说明持仓情况和资料中的市净率及其适用主体。"),
    "compound_individual_scope": (_position_prose + "600928 西安银行的市净率为0.8倍。", True, _holding,
                                 "说明持仓情况和资料中的市净率及其适用主体。"),
    "compound_absence_as_nonexecution": (_price_prose + "本轮未执行新的买卖操作。", False, _prices,
                                        "说明两日收盘价和现有资料能否确认本轮买卖执行情况。"),
    "compound_absence_as_unknown": (_price_prose + "没有本轮交易回执，无法确认本轮是否执行了买卖。", True, _prices,
                                   "说明两日收盘价和现有资料能否确认本轮买卖执行情况。"),
    "reporting_is_not_nonexecution": (_price_prose + "本文不报告成交情况，不能据此认定本轮没有交易。", True, _prices,
                                     "说明两日收盘价，并保持本轮交易状态的不确定性。"),
    "reporting_rule_with_noop": (_price_prose + "本文不报告成交情况，因此本轮没有交易。", False, _prices,
                                "说明两日收盘价，并保持本轮交易状态的不确定性。"),
    "policy_hold_decision": (_hold_decision, True, _held_prices, _hold_rule),
    "policy_hold_reordered": (_price_prose + "持有002415共100股。由于两日收盘价走高，按仅下跌才考虑买入的规则，"
                              "本轮选择继续观察已有仓位，不新增买入。", True, _held_prices, _hold_rule),
    "policy_context_mismatch": (_hold_decision, False, _held_prices,
                                "仅在所给两个观察日收盘价上升时才考虑新增买入。报告持仓、两日价格变化、处理决定和依据。"),
    "decision_with_nonexecution": (_price_prose + "本轮决定继续观察，本轮没有提交买单。", False, _prices,
                                    "根据两日行情给出观察决定；执行状态只能按真实回执确认。"),
    "decision_with_fill": (_price_prose + "本轮决定继续观察，且已经买入002415共100股。", False, _prices,
                           "根据两日行情给出观察决定；执行状态只能按真实回执确认。"),
    "request_cause_is_not_evidence": (_price_prose + "营收增长导致了这次上涨。", False, _prices,
                                     "我认为营收增长导致了上涨。说明两日收盘价及这项因果判断是否有依据。"),
    "request_price_is_not_evidence": ("002415在2025-01-02的收盘价为30元/股。", False, _prices,
                                     "已知002415在2025-01-02的收盘价为30元/股。请核对并报告该日收盘价。"),
    "bounded_observed_direction": (_bounded_direction, True, _learning_prices, "说明两个观察日收盘价的变化方向。"),
    "bounded_direction_reordered": ("002415在2026-11-10至2026-11-11这两个观察日内的收盘价呈上升趋势。",
                                     True, _learning_prices, "说明两个观察日收盘价的变化方向。"),
    "bounded_direction_reversed": (_bounded_direction, False, _learning_reversed, "说明两个观察日收盘价的变化方向。"),
    "persistent_direction": ("基于2026-11-10至2026-11-11的数据，可以确定002415的上升趋势未来仍将持续。",
                              False, _learning_prices, "说明这两次收盘观察能支持什么结论。"),
    "statistical_significance": ("2026-11-10至2026-11-11的两个收盘价证明002415的上涨具有统计显著性。",
                                  False, _learning_prices, "说明这两次收盘观察能支持什么结论。"),
    'actor_absence_from_activity': (_activity_prose + '这些统计证明非主力出货。', False, _activity,
                                    '说明成交活跃度及能否判断交易主体的卖出行为。'),
    'actor_activity_limited': (_activity_prose + '成交活跃度较低，但仅这些统计不足以判断交易主体是否卖出。',
                              True, _activity, '说明成交活跃度及能否判断交易主体的卖出行为。'),
    'actor_absence_with_limitation': (_activity_prose + '虽然不能确定后续走势，但可以确定该观察日非主力出货。',
                                     False, _activity, '说明成交活跃度及资料的适用限制。'),
    'actor_zero_missing': (_actor_zero, False, _activity, '说明成交活跃度及统计覆盖范围内的机构卖出金额。'),
    'actor_zero_supported': (_actor_zero, True, [*_activity, *_actor_statistics],
                             '说明成交活跃度及统计覆盖范围内的机构卖出金额。'),
    'compound_actor_from_activity': (_long_activity_prose +
        '结合上述开盘价、收盘价及0.594倍量比，可以确定该日非主力出货。', False, _long_activity,
        '报告所给日线价量特征，并说明能否确定卖方身份。'),
    'compound_activity_limited': (_long_activity_prose +
        '量比低于1倍，但仅凭所给价量资料，无法识别卖出方是否属于某类主力资金。', True, _long_activity,
        '报告所给日线价量特征，并说明能否确定卖方身份。'),
    'bound_percentage_display': ('金融行业的涨跌幅为0.42%。', True, _market_percentage,
                                 '说明金融行业的涨跌幅。'),
    'wrong_percentage_display': ('金融行业的涨跌幅为0.43%。', False, _market_percentage,
                                 '说明金融行业的涨跌幅。'),
    'prospective_watchpoint': (_price_prose +
        '建议继续观察。若之后收盘价超过24元/股，再重新评估是否增持；这只是后续观察条件。',
        True, _prices, '报告所给两个观察日的收盘价，并提出一个后续观察条件，不要求本轮执行交易。'),
    'watchpoint_with_false_observation': (_price_prose +
        '当前这两项资料的后一日收盘价已经超过24元/股，所以建议继续观察。',
        False, _prices, '报告所给两个观察日的收盘价，并提出一个后续观察条件，不要求本轮执行交易。'),
    'watchpoint_with_fictitious_fill': (_price_prose +
        '若之后收盘价超过24元/股，再重新评估是否增持。本轮已经买入002415共100股。',
        False, _prices, '报告所给两个观察日的收盘价，并提出一个后续观察条件，不要求本轮执行交易。'),
    'thesis_judgment_supported': ('按2025年1月2日的数据，600928市净率为0.8倍。'
        '我认为估值折价这一持有理由仍可保留，但仅凭该指标不能保证安全边际或后续收益。',
        True, _company, '说明所给市净率，评估估值折价是否仍可作为持有理由，并说明判断的限制。'),
    'thesis_judgment_reordered': ('仅这一项估值指标不足以保证安全边际或收益；'
        '不过，600928在2025年1月2日的市净率为0.8倍，依此分析，估值折价的持有理由仍可保留。',
        True, _company, '说明所给市净率，评估估值折价是否仍可作为持有理由，并说明判断的限制。'),
    'thesis_judgment_false_premise': ('按2025年1月2日的数据，600928市净率为0.8倍。'
        '我认为估值折价这一持有理由仍可保留，但仅凭该指标不能保证安全边际或后续收益。',
        False, [('get_fundamentals', json.dumps({'symbol': '600928', 'name': '西安银行',
            'as_of': '2025-01-02', 'pb': 1.8}))],
        '说明所给市净率，评估估值折价是否仍可作为持有理由，并说明判断的限制。'),
    'thesis_judgment_overclaim': ('按2025年1月2日的数据，600928市净率为0.8倍。'
        '我认为这个估值足以保证持有不会亏损。', False, _company,
        '说明所给市净率，评估估值折价是否仍可作为持有理由，并说明判断的限制。'),
    "current_execution_receipt": (_price_prose + "本轮已买入002415共100股，成交价22元/股。", True,
        [*_prices, ("place_paper_trade", json.dumps({"status": "executed", "trade_id": "frozen-current-fill",
            "symbol": "002415", "account": "test", "side": "buy", "quantity": 100, "price": 22,
            "timestamp": "2025-01-02T10:00:00+08:00"}))], "说明两日收盘价和本轮执行结果。"),
}
CASES.update({name: (prose, expected) for name, (prose, expected, _, _) in SCOPE_CASES.items()})

_dated_price = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2025-01-02", "close": 22}]}))]
_utc_price = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2025-01-01T16:00:00+00:00", "close": 22}]}))]
_current_valuation_prose = ('600928 西安银行。基本面方面，公司当前市净率为0.4572倍，'
                            '深度破净状态提供了较高的估值安全边际。')
# Frozen producer-shaped evidence, explicitly not a live provider check. The
# producer's "sample" flag excludes observations and would invalidate this pair.
_valuation_base = {'_meta': {'source': 'tushare', 'timestamp': '2026-11-12T15:10:51', 'freshness': 'realtime'},
                   'provider': 'tushare', 'has_real_fundamentals': True, 'data_status': 'available',
                   'symbol': '600928', 'name': '西安银行', 'pb': 0.4572}
_fresh_valuation = [('get_fundamentals', json.dumps({**_valuation_base, 'as_of': '20261112'}))]
_stale_valuation = [('get_fundamentals', json.dumps({**_valuation_base, 'as_of': '20261111'}))]
_cross_calendar_quote = [('sina_realtime_quote', json.dumps({
    'symbol': '002415', 'price': 22, 'timestamp': '2025-01-01T16:00:00+00:00'}))]
_basic_date_price = [('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d',
    'bars': [{'timestamp': '20250102', 'close': 22}]}))]
BASIC_DATE_CASES = ('utc_basic_date_current', 'utc_basic_date_stale')
QUOTE_CALENDAR_CASES = ('utc_current_observation', 'utc_stale_observation')
_qualitative_bars = [('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d',
    'bars': [{'timestamp': '2025-01-02', 'high': 23, 'low': 22, 'close': 22.5}]}))]
_ma_mixed = [('compute_ma', json.dumps({'symbol': '002415', 'timeframe': '1d',
    '_meta': {'as_of': '2025-01-02'}, 'ma': {'MA5': 22, 'MA10': 23, 'MA20': 21, 'MA60': 20},
    'trend': 'mixed'}))]
_ma_bullish = [('compute_ma', json.dumps({'symbol': '002415', 'timeframe': '1d',
    '_meta': {'as_of': '2025-01-02'}, 'ma': {'MA5': 23, 'MA10': 22, 'MA20': 21, 'MA60': 20},
    'trend': 'bullish_alignment'}))]
QUALITATIVE_TIME_CASES = ('qualitative_current', 'qualitative_stale')
MA_CLASSIFICATION_CASES = ('ma_mixed_as_bullish', 'ma_bullish')
TIME_CASES = {
    "relative_current": ("今天002415收盘价为22元/股。", True, _dated_price, "2025-01-02"),
    "relative_stale": ("今天002415收盘价为22元/股。", False, _dated_price, "2025-01-03"),
    "relative_unknown": ("今天002415收盘价为22元/股。", False, _dated_price, None),
    "utc_display_date": ("002415在2025-01-02的收盘价为22元/股。", True, _utc_price, "2025-01-03"),
    "utc_wrong_date": ("002415在2025-01-01的收盘价为22元/股。", False, _utc_price, "2025-01-03"),
    'pb_fresh_current': (_current_valuation_prose, True, _fresh_valuation, '2026-11-12'),
    'pb_stale_current': (_current_valuation_prose, False, _stale_valuation, '2026-11-12'),
    'pb_stale_explicit': (_current_valuation_prose.replace('公司当前市净率',
        '按2026年11月11日的数据，公司市净率'), True, _stale_valuation, '2026-11-12'),
    'utc_current_observation': ('002415当前报价为22元/股。', True, _cross_calendar_quote, '2025-01-01'),
    'utc_stale_observation': ('002415当前报价为22元/股。', False, _cross_calendar_quote, '2025-01-02'),
    'utc_basic_date_current': ('002415今天的收盘价为22元/股。', True, _basic_date_price, '2025-01-02'),
    'utc_basic_date_stale': ('002415今天的收盘价为22元/股。', False, _basic_date_price, '2025-01-03'),
    'qualitative_current': ('002415今天盘中存在价格起伏。', True, _qualitative_bars, '2025-01-02'),
    'qualitative_stale': ('002415今天盘中存在价格起伏。', False, _qualitative_bars, '2025-01-03'),
    'ma_mixed_as_bullish': ('002415的均线已形成多头排列。', False, _ma_mixed, '2025-01-02'),
    'ma_bullish': ('002415的均线已形成多头排列。', True, _ma_bullish, '2025-01-02'),
}
CASES.update({name: (prose, expected) for name, (prose, expected, _, _) in TIME_CASES.items()})
VALUATION_TIME_CASES = ('pb_fresh_current', 'pb_stale_current', 'pb_stale_explicit')
TIME_CASE_TASKS = {name: '根据所给资料说明600928的市净率及所述估值判断。'
                   for name in VALUATION_TIME_CASES}
TIME_CASE_TASKS.update({name: '按任务时区UTC，根据所给资料说明002415当前报价。'
                       for name in ('utc_current_observation', 'utc_stale_observation')})
TIME_CASE_TASKS.update({name: '按任务时区UTC，根据所给资料说明002415今天的收盘价。'
                       for name in BASIC_DATE_CASES})
TIME_CASE_TASKS.update({name: '仅依据所给观察资料说明002415今天盘中是否存在价格起伏。'
                       for name in QUALITATIVE_TIME_CASES})
TIME_CASE_TASKS.update({name: '仅依据所给均线资料判断002415的均线是否已形成多头排列。'
                       for name in MA_CLASSIFICATION_CASES})
TIME_CASE_TIMEZONES = {name: 'UTC' for name in (*QUOTE_CALENDAR_CASES, *BASIC_DATE_CASES)}


def qualitative_fixture_checks(selected):
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    from trade_compass_agent.runtime.tools.ta import _ma_trend_summary
    result = {}
    for name in selected:
        if name not in (*QUALITATIVE_TIME_CASES, *MA_CLASSIFICATION_CASES):
            continue
        receipts = TIME_CASES[name][2]
        catalog = EvidenceCatalog.build(receipts, run_scope='qualitative-fixture')
        observed = [fact for fact in catalog.facts.values() if fact.kind != 'identity']
        assert observed and {fact.observation_date('Asia/Shanghai') for fact in observed} == {'2025-01-02'}
        source = json.loads(receipts[0][1])
        if name in MA_CLASSIFICATION_CASES:
            assert _ma_trend_summary(source['ma']) == source['trend']
            assert len([fact for fact in observed if fact.kind == 'moving_average']) == 4
        else:
            assert source['bars'][0]['high'] > source['bars'][0]['low']
        result[name] = {'source_observation_date': '2025-01-02', 'source_unchanged': True,
                        'classification': source.get('trend')}
    return result


def valuation_fixture_checks(selected):
    """Require the intended fact before a negative can prove anything about time."""
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    checked = {}
    for name in selected:
        if name not in VALUATION_TIME_CASES:
            continue
        catalog = EvidenceCatalog.build(TIME_CASES[name][2], run_scope='fixture-preflight')
        facts = [f for f in catalog.facts.values() if f.entity == '600928' and f.metric == 'pb']
        expected_date = '2026-11-12' if name == 'pb_fresh_current' else '2026-11-11'
        if (len(facts) != 1 or facts[0].value != '0.4572' or facts[0].unit != '倍'
                or facts[0].observation_date('Asia/Shanghai') != expected_date):
            raise RuntimeError(f'{name}: frozen valuation fixture lost its value, unit or observation date')
        checked[name] = {'value': facts[0].value, 'unit': facts[0].unit,
                         'observed_at': facts[0].observed_at, 'observed_date': expected_date}
    return checked


def quote_fixture_checks(selected):
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    checked = {}
    for name in selected:
        if name not in QUOTE_CALENDAR_CASES:
            continue
        catalog = EvidenceCatalog.build(TIME_CASES[name][2], run_scope='fixture-preflight')
        facts = [f for f in catalog.facts.values() if f.entity == '002415' and f.metric == 'price']
        if (len(facts) != 1 or facts[0].value != '22' or facts[0].unit != '元/股'
                or facts[0].observation_date('UTC') != '2025-01-01'
                or facts[0].observation_date('Asia/Shanghai') != '2025-01-02'):
            raise RuntimeError(f'{name}: frozen quote lost its value or cross-calendar instant')
        checked[name] = {'observed_at': facts[0].observed_at, 'task_calendar_date': '2025-01-01',
                         'display_calendar_date': '2025-01-02'}
    return checked


def basic_date_fixture_checks(selected):
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    checked = {}
    for name in selected:
        if name not in BASIC_DATE_CASES:
            continue
        catalog = EvidenceCatalog.build(TIME_CASES[name][2], run_scope='fixture-preflight')
        facts = [f for f in catalog.facts.values() if f.entity == '002415' and f.metric == 'close']
        if (len(facts) != 1 or facts[0].value != '22' or facts[0].unit != '元/股'
                or facts[0].observed_at != '2025-01-02'
                or facts[0].observation_date('UTC') != '2025-01-02'):
            raise RuntimeError(f'{name}: basic date lost its date-only precision or source value')
        checked[name] = {'observed_at': facts[0].observed_at, 'invented_instant': False}
    return checked


def finalize_acceptance(result, selected_cases):
    """Summarize named consumer results without confusing them with body rows."""
    selected_cases = list(selected_cases)
    completed = [row['case'] for row in result['cases']]
    return {**result,
        'not_run': [name for name in selected_cases if name not in completed],
        'passed': bool(selected_cases) and len(completed) == len(set(completed)) == len(selected_cases)
                  and set(completed) == set(selected_cases) and all(row['passed'] is True for row in result['cases'])}


def qualitative_time_check_completed(first, supported):
    """Require a positive witness or a completed temporal rejection, never unavailability."""
    if not first['review_valid'] or first.get('factual_assessment_errors'):
        return False
    expected_date = '2025-01-02' if supported else '2025-01-03'
    timing = [check for check in first.get('temporal_checks', {}).values()
              if check['scope'] == 'reference_date' and check['expected_date'] == expected_date]
    linked = any(check['status'] == ('entailed' if supported else 'unknown')
                 and any(row['comparison_date'] == '2025-01-02' for row in check['evidence'])
                 for check in timing)
    if supported:
        return linked
    # A complete assessor may identify the date mismatch itself. Evidence
    # supporting the asserted current date does not exist in this negative;
    # an empty supporting-witness list does not erase its completed rejection.
    semantic = any(row.get('reported_checks', row['checks'])['time'] in {'unknown', 'contradicted'}
                   for row in (first.get('factual_review') or {}).get('assessments', []))
    return bool(timing) and (linked or semantic)


REJECTION_TARGETS = {
    'ma_mixed_as_bullish': ('均线呈多头排列', 'support', {'unknown', 'contradicted'}),
    'ma_bullish_denied': ('均线尚未构成多头排列', 'support', {'unknown', 'contradicted'}),
    'thesis_judgment_false_premise': ('600928市净率为0.8倍', 'quantity', {'contradicted'}),
    'thesis_judgment_overclaim': ('这个估值足以保证持有不会亏损', 'support', {'unknown', 'contradicted'}),
    'actor_absence_from_activity': ('这些统计证明非主力出货', 'support', {'unknown', 'contradicted'}),
    'actor_absence_with_limitation': ('可以确定该观察日非主力出货', 'support', {'unknown', 'contradicted'}),
    'actor_zero_missing': ('机构卖出额为0元', 'support', {'unknown', 'contradicted'}),
    'compound_actor_from_activity': ('可以确定该日非主力出货', 'support', {'unknown', 'contradicted'}),
}


def target_rejection_check(first, name):
    """Locate a frozen test predicate; an unrelated rejection cannot pass it.

    Exact text locates the known fixture only. The real assessor's completed
    dimensional verdict decides whether that predicate was correctly blocked.
    This helper does not interpret or classify production prose.
    """
    quote, dimension, required = REJECTION_TARGETS[name]
    statements = (first.get('factual_review_input') or {}).get('statements', [])
    contexts = dict.fromkeys(row['context_id'] for row in statements)
    targets = []
    occurrences = 0
    for context_id in contexts:
        rows = [row for row in statements if row['context_id'] == context_id]
        text = ''.join(row['text'] for row in rows)
        occurrences += text.count(quote)
        start = text.find(quote)
        if start < 0:
            continue
        offset = 0
        for row in rows:
            end = offset + len(row['text'])
            if offset < start + len(quote) and end > start:
                targets.append(row['id'])
            offset = end
    assessments = {row['id']: row for row in (first.get('factual_review') or {}).get('assessments', [])}
    errors = first.get('factual_assessment_errors', {})
    completed = (first.get('review_valid') is True and occurrences == 1 and bool(targets)
                 and all(key in assessments and key not in errors for key in targets))
    statuses = [assessments[key]['checks'][dimension] for key in targets if key in assessments]
    return {'passed': completed and any(status in required for status in statuses),
            'target_assertion_ids': targets, 'dimension': dimension,
            'target_completed': completed, 'target_statuses': statuses}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--fact-model", default="", help="Independent local LM Studio factual reviewer")
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES),
                        help="Run named controls and record unselected cases explicitly")
    args = parser.parse_args()
    selected = dict.fromkeys(args.cases or CASES)
    root = args.output.resolve()
    wheel = args.wheel.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise RuntimeError("acceptance must run outside checkout")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chdir(root)
    for key in list(os.environ):
        if key.startswith("TRADE_COMPASS_"):
            del os.environ[key]
    (root / "config.yaml").write_text("llm:\n  provider: disabled\ndata_provider: sample\n")
    (root / ".env").touch()
    os.environ.update(TRADE_COMPASS_HOME=str(root), TRADE_COMPASS_CONFIG=str(root/"config.yaml"),
                      TRADE_COMPASS_ENV_FILE=str(root/".env"), TRADE_COMPASS_DATA_DIR=str(root/"data"),
                      TRADE_COMPASS_MEMORY_DIR=str(root/"memory"), TRADE_COMPASS_NO_SCHEDULER="true")
    from trade_compass_agent.config import AppConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client, create_local_report_fact_check, llm_request_deadline
    from trade_compass_agent.runtime.report_publication import publish_report, _json_value

    verify = runpy.run_path(str(Path(__file__).with_name("release_acceptance.py")))["verify_installed_wheel"]
    result = {"passed": False, "artifact": verify(wheel), "mode": "frozen first prose / real review and bounded normal revision",
              "model": args.model, "fact_model": args.fact_model, "cases": [], "plan": CASES, "retries": 0, "business_calls": 0,
              "selected_cases": list(selected), "unselected_cases": [name for name in CASES if name not in selected],
              "full_corpus": len(selected) == len(CASES),
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "fixture_checks": {**valuation_fixture_checks(selected), **quote_fixture_checks(selected),
                                 **basic_date_fixture_checks(selected), **qualitative_fixture_checks(selected)}}
    (root/"plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    config = AppConfig(data_dir=root/"data", memory_dir=root/"memory",
                       llm=LLMConfig(provider="lmstudio", model=args.model, api_key_env="", max_retries=0, timeout=120,
                                     local_report_review_model=args.fact_model))
    client = create_chat_client(config)
    factual_client = create_local_report_fact_check(config)
    receipts = [
        ("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
            {"timestamp": "2025-01-01", "close": 22}, {"timestamp": "2025-01-02", "close": 23}]})),
        ("analyze_portfolio", json.dumps({"recent_trades": [{"symbol": "002415", "account": "test",
            "trade_id": "historical-fill", "side": "buy", "quantity": 100, "price": 22,
            "timestamp": "2024-12-31T14:00:00+08:00"}]})),
    ]
    for name in selected:
        prose, expected = CASES[name]
        case_receipts = list(TIME_CASES[name][2] if name in TIME_CASES else
                             SCOPE_CASES[name][2] if name in SCOPE_CASES else receipts)
        if name == "changed_direction_evidence":
            changed = json.loads(case_receipts[0][1])
            changed["bars"][0]["close"], changed["bars"][1]["close"] = 23, 22
            case_receipts[0] = ("get_bars", json.dumps(changed))
        normal_publication = expected and name != "hidden_result"
        # Both outcomes need the same first body/coverage/evidence path as the
        # 240-second full task harness. Expected truth must not halve its time.
        # Negative controls still cannot rewrite or resample their first verdict.
        deadline_seconds = 240
        primary_limit = 4 if normal_publication else 2
        calls, factual_calls, writing_calls = [], [], []
        def factual_call(system, user):
            payload = json.loads(user)
            row = {"input": payload, "stage": "body_meaning" if "paragraphs" in payload else "entailment"}
            factual_calls.append(row)
            row["output"] = factual_client(system, user)
            return row["output"]
        def call(system, user):
            payload = json.loads(user)
            if "schema" in payload:
                writing_calls.append({"input": payload, "frozen": "revision" not in payload})
                if "revision" in payload:
                    if not normal_publication or len(writing_calls) != 2:
                        raise RuntimeError("only one normal publication revision is allowed")
                    answer = client.stream_complete([ChatMessage(role="system", content=system),
                        ChatMessage(role="user", content=user)], tools=None)
                    writing_calls[-1]["output"] = answer.content
                    if answer.tool_calls:
                        raise RuntimeError("revision requested a tool")
                    return answer.content
                if len(writing_calls) != 1:
                    raise RuntimeError("frozen first composition cannot be resampled")
                if name in SCOPE_CASES or name in TIME_CASES:
                    refs = [row[0] for group in payload["facts"] for row in group["rows"]]
                    parts = [{"type": "text", "text": prose}]
                    if name == "utc_display_date":
                        close_id = next(group["rows"][0][0] for group in payload["facts"] if group["metric"] == "close")
                        parts = [{"type": "text", "text": "002415在"},
                                 {"type": "value", "ref": close_id, "field": "date"},
                                 {"type": "text", "text": "的收盘价为22元/股。"}]
                    if name in QUOTE_CALENDAR_CASES:
                        price_id = next(group['rows'][0][0] for group in payload['facts'] if group['metric'] == 'price')
                        parts = [{'type': 'text', 'text': '002415当前报价为'},
                                 {'type': 'value', 'ref': price_id, 'field': 'value_with_unit'},
                                 {'type': 'text', 'text': '。'}]
                    if name in BASIC_DATE_CASES:
                        close_id = next(group['rows'][0][0] for group in payload['facts'] if group['metric'] == 'close')
                        parts = [{'type': 'text', 'text': '002415今天的收盘价为'},
                                 {'type': 'value', 'ref': close_id, 'field': 'value_with_unit'},
                                 {'type': 'text', 'text': '。'}]
                    items = [{"id": "answer", "section": "overview", "type": "paragraph", "parts": parts, "refs": refs}]
                    if name == 'bound_percentage_display':
                        pct = next(group['rows'][0][0] for group in payload['facts'] if group['metric'] == 'change_pct')
                        items[0]['parts'] = [{'type': 'value', 'ref': pct, 'field': 'subject'},
                            {'type': 'text', 'text': '的涨跌幅为'}, {'type': 'value', 'ref': pct, 'field': 'value_with_unit'},
                            {'type': 'text', 'text': '。'}]
                    if name in {"bound_duplicate_gap", "bound_genuine_gap"}:
                        first = next(group["rows"][0][0] for group in payload["facts"] if group["metric"] == "close")
                        items[0]["parts"] = [
                            {"type": "value", "ref": first, "field": "subject"}, {"type": "text", "text": "在"},
                            {"type": "value", "ref": first, "field": "date"}, {"type": "text", "text": "的收盘价为"},
                            {"type": "value", "ref": first, "field": "value_with_unit"}, {"type": "text", "text": "。"}]
                        items.append({"id": "gap", "section": "overview", "type": "gap",
                            "text": "缺少营收资料。" if name == "bound_genuine_gap" else "缺少002415在2025年1月1日的收盘价。"})
                    return json.dumps({"schema_version": 1, "items": items}, ensure_ascii=False)
                facts = [dict(zip(payload["fact_columns"], row, strict=True))
                         for group in payload["facts"] if group["metric"] == "close" for row in group["rows"]]
                parts = [{"type": "value", "ref": facts[0]["id"], "field": "subject"},
                         {"type": "text", "text": "的收盘价从"},
                         {"type": "value", "ref": facts[0]["id"], "field": "value_with_unit"},
                         {"type": "text", "text": "变为"},
                         {"type": "value", "ref": facts[1]["id"], "field": "value_with_unit"},
                         {"type": "text", "text": "。" + prose}]
                historical_refs = [row[0] for group in payload["facts"]
                                   if group["source"] == "analyze_portfolio" for row in group["rows"]]
                if name == "historical_execution" and not historical_refs:
                    raise RuntimeError("historical execution fixture lost its source")
                definitions = []
                refs = historical_refs if name == "historical_execution" else []
                if name in {"hidden_result", "compact_percentage", "visible_result", "missing_percentage_unit", "correct_percentage",
                            "unit_in_body_context", "explicit_ratio_scale", "percentage_as_price", "wrong_rounded_percentage"}:
                    definitions = [{"type": "calculation", "id": "change", "section": "overview",
                                    "operation": "change_pct", "refs": [facts[0]["id"], facts[1]["id"]]}]
                    refs = ["change"]
                    if name == "visible_result":
                        parts.extend([{"type": "text", "text": "这两日收盘价上涨了"},
                                      {"type": "value", "ref": "change", "field": "value_with_unit"},
                                      {"type": "text", "text": "。"}])
                if name == "compact_percentage":
                    parts = [{"type": "value", "ref": facts[0]["id"], "field": "subject"},
                             {"type": "text", "text": "收盘价："},
                             {"type": "value", "ref": facts[0]["id"], "field": "value_with_unit"},
                             {"type": "text", "text": " → "},
                             {"type": "value", "ref": facts[1]["id"], "field": "value_with_unit"},
                             {"type": "text", "text": "（+"},
                             {"type": "value", "ref": "change", "field": "value_with_unit"},
                             {"type": "text", "text": "）。"}]
                return json.dumps({"schema_version": 1, "items": [*definitions,
                    {"type": "paragraph", "id": "answer", "section": "overview", "parts": parts,
                     "refs": refs}]})
            if len(calls) >= (2 if normal_publication else 1):
                raise RuntimeError("review call budget exceeded")
            calls.append({"input": payload})
            answer = client.stream_complete([ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user)], tools=None)
            calls[-1]["output"] = answer.content
            if answer.tool_calls:
                raise RuntimeError("review requested a tool")
            return answer.content
        entry = {"case": name, "expected_supported": expected, "passed": False,
                 "deadline_seconds": deadline_seconds, "primary_call_limit": primary_limit,
                 "revision_allowed": normal_publication}
        started = time.monotonic()
        try:
            with llm_request_deadline(started + deadline_seconds):
                task = (TIME_CASE_TASKS.get(name, "说明所给日线资料中的收盘价及其日期。") if name in TIME_CASES else
                        SCOPE_CASES[name][3] if name in SCOPE_CASES else
                        "说明这两个观察日收盘价变化，仅在存在本轮执行记录时报告成交。")
                if name in {"hidden_result", "compact_percentage", "visible_result", "missing_percentage_unit", "correct_percentage",
                            "unit_in_body_context", "explicit_ratio_scale", "percentage_as_price", "wrong_rounded_percentage"}:
                    task += "给出收盘价变动比例。"
                report = publish_report(task=task,
                    draft="", tool_results=case_receipts, current_tool_results=case_receipts,
                    run_scope="semantic-"+name, call=call, max_calls=primary_limit,
                    fact_review_call=factual_call if factual_client else None,
                    reference_date=TIME_CASES[name][3] if name in TIME_CASES else None,
                    reference_timezone=TIME_CASE_TIMEZONES.get(name, 'Asia/Shanghai'))
            (root/(name+"-artifact.json")).write_text(json.dumps(report.artifact, ensure_ascii=False, indent=2))
            reviews = (report.artifact.get("review") or {}).get("reviews", [])
            actual = next((r["supported"] for r in reviews if r["id"] == "answer"), None)
            attempts = report.artifact.get("revision_attempts", [report.artifact])
            first = attempts[0]
            first_reviews = (first.get("review") or {}).get("reviews", [])
            first_supported = next((r["supported"] for r in first_reviews if r["id"] == "answer"), None)
            paragraph_ids = {item["id"] for item in report.artifact["document"]["items"] if item["type"] == "paragraph"}
            supported_ids = {row["id"] for row in reviews if row["supported"]}
            supported_ids.update(key for key, projection in report.artifact["execution_projections"].items()
                                 if projection.get("analysis_supported"))
            final_supported = bool(paragraph_ids) and paragraph_ids <= supported_ids
            first_prose = [row["text"] for row in (first.get("body_review_input") or {}).get("paragraphs", [])]
            final_prose = [row["text"] for row in (report.artifact.get("body_review_input") or {}).get("paragraphs", [])]
            revised = len(attempts) == 2
            # Changing IDs or asking the reviewer again is not a correction.
            changed_prose = first_prose != final_prose if factual_client else first["document"] != report.artifact["document"]
            coverage_complete = (report.artifact.get("review") or {}).get("coverage", {}).get("complete")
            local_calls = report.artifact["factual_model_calls"] + report.artifact["body_review_model_calls"]
            selector_calls = 0
            for row in factual_calls:
                if row.get('stage') == 'body_meaning' and row.get('output'):
                    body_paragraphs = _json_value(row['output']).get('paragraphs', [])
                    selector_calls += bool(body_paragraphs) and all(
                        paragraph.get('parts') and all('through' in part for part in paragraph['parts'])
                        for paragraph in body_paragraphs)
            entry.update(actual_supported=actual, coverage_complete=coverage_complete, status=report.verification["status"],
                         first_supported=first_supported, final_supported=final_supported,
                         revision_attempted=revised, selected_attempt=report.artifact.get("selected_attempt", 0),
                         changed_prose=changed_prose, model_calls=report.artifact["model_calls"],
                         body_model_calls=report.artifact["body_review_model_calls"],
                         factual_model_calls=report.artifact["factual_model_calls"],
                         unavailable_assertions=len(report.artifact.get('factual_assessment_errors', {})),
                         body_selector_calls=selector_calls,
                         passed=report.artifact["review_valid"]
                         and not report.artifact.get('factual_assessment_errors')
                         and (final_supported if normal_publication else actual is expected)
                         and 1 <= len(calls) <= (2 if normal_publication else 1)
                         and len(factual_calls) == local_calls
                         and report.artifact["model_calls"] == len(calls) + len(writing_calls) + local_calls
                         and report.artifact["model_calls"] <= primary_limit + 4 * int(bool(factual_client))
                         and report.artifact["body_review_model_calls"] == (1 + int(revised)) * int(bool(factual_client))
                         and (not revised or changed_prose and report.artifact.get("selected_attempt") == 1)
                         and ((coverage_complete is False and report.verification["report"] == "partial")
                              if name == "hidden_result" else
                              (report.verification["status"] == "incomplete") if name == "bound_genuine_gap" else
                              (report.verification["status"] == "complete") if expected else
                              (prose not in report.text
                               and report.verification["report"] == "partial")))
            if name in {"bound_duplicate_gap", "bound_genuine_gap"}:
                gap = (report.artifact.get("review") or {}).get("gap_reviews", [])
                facts_by_id = {row["id"]: row for row in report.artifact["facts"]}
                price_visible = any(
                    part.get("field") == "value_with_unit"
                    and (fact := facts_by_id.get(part.get("ref"), {})).get("subject") == "002415"
                    and fact.get("metric") == "close" and str(fact.get("value")) == "22"
                    and fact.get("observed_at", "").startswith("2025-01-01")
                    for item in report.artifact["document"]["items"] if item["type"] == "paragraph"
                    for part in item["parts"])
                correct_gap = len(gap) == 1 and gap[0]["id"] == "gap" and gap[0]["status"] == (
                    "required" if name == "bound_genuine_gap" else "already_present")
                if name == "bound_genuine_gap":
                    entry["passed"] = (entry["passed"] and final_supported and correct_gap and price_visible
                        and report.verification["status"] == "incomplete" and "缺少营收资料" in report.text
                        and any(f["code"] == "declared_gap" for f in report.verification["findings"]))
                else:
                    entry["passed"] = entry["passed"] and price_visible and (correct_gap or revised and not gap)
            if name in {"missing_percentage_unit", "currency_missing"} and factual_client:
                # The body-only boundary must catch this, even if evidence
                # entailment would silently supply the omitted reader unit.
                entry["passed"] = entry["passed"] and bool(report.artifact["body_quantity_issues"].get("answer"))
                entry["passed"] = entry["passed"] and report.artifact["factual_model_calls"] == 0
            if name == "current_execution_receipt" and factual_client:
                fill = next(json.loads(row["result"]) for row in report.artifact["receipts"]
                            if row["tool"] == "place_paper_trade")
                entry["passed"] = entry["passed"] and fill["quantity"] == 100
                entry["passed"] = entry["passed"] and report.artifact["execution_records"][0]["status"] == "executed"
                entry["passed"] = entry["passed"] and "买入100股" in report.text
            if name == 'pb_stale_current' and factual_client:
                pb_ids = {fact['id'] for fact in first['facts'] if fact['metric'] == 'pb'}
                timing = list(first.get('temporal_checks', {}).values())
                correct_time_rejection = any(
                    check['scope'] == 'reference_date' and check['expected_date'] == '2026-11-12'
                    and check['status'] == 'unknown' and check['reason'] == 'observation_not_at_asserted_date'
                    and any(row['ref'] in pb_ids and row['observed_date'] == '2026-11-11'
                            for row in check['evidence']) for check in timing)
                entry['runtime_time_rejection'] = correct_time_rejection
                entry['passed'] = entry['passed'] and correct_time_rejection
            if name in QUALITATIVE_TIME_CASES and factual_client:
                completed_check = qualitative_time_check_completed(first, expected)
                entry['first_temporal_check_completed'] = completed_check
                entry['passed'] = entry['passed'] and completed_check
            if name == 'ma_mixed_as_bullish' and factual_client:
                correct_support_rejection = any(row['checks']['support'] in {'unknown', 'contradicted'}
                    for row in (first.get('factual_review') or {}).get('assessments', []))
                entry['classification_support_rejection'] = correct_support_rejection
                entry['passed'] = entry['passed'] and correct_support_rejection
            if name in REJECTION_TARGETS and factual_client:
                target_check = target_rejection_check(first, name)
                entry['target_rejection_check'] = target_check
                entry['passed'] = entry['passed'] and target_check['passed']
            if name == 'utc_stale_observation' and factual_client:
                correct_time_rejection = any(
                    check['comparison_timezone'] == 'UTC' and check['expected_date'] == '2025-01-02'
                    and check['status'] == 'unknown' and check['reason'] == 'observation_not_at_asserted_date'
                    and any(row['comparison_date'] == '2025-01-01' and row['observed_date'] == '2025-01-02'
                            for row in check['evidence']) for check in first.get('temporal_checks', {}).values())
                entry['runtime_time_rejection'] = correct_time_rejection
                entry['passed'] = entry['passed'] and correct_time_rejection
            if name == 'utc_basic_date_stale' and factual_client:
                correct_time_rejection = any(
                    check['comparison_timezone'] == 'UTC' and check['expected_date'] == '2025-01-03'
                    and check['status'] == 'unknown' and check['reason'] == 'observation_not_at_asserted_date'
                    and any(row['comparison_date'] == row['observed_date'] == '2025-01-02'
                            for row in check['evidence']) for check in first.get('temporal_checks', {}).values())
                entry['runtime_time_rejection'] = correct_time_rejection
                entry['passed'] = entry['passed'] and correct_time_rejection
            if name in {"historical_execution", "absence_as_nonexecution", "compound_absence_as_nonexecution", "reporting_rule_with_noop"} and factual_client:
                projection = report.artifact["execution_projections"].get("answer", {})
                entry["passed"] = entry["passed"] and projection.get("has_execution_records") is False
                if name == "compound_absence_as_nonexecution":
                    entry["passed"] = entry["passed"] and projection.get("analysis_supported") is True and _price_prose in report.text
        except Exception as exc:
            entry["error"] = str(exc)
        entry["seconds"] = round(time.monotonic() - started, 2)
        (root/(name+"-calls.json")).write_text(json.dumps(calls, ensure_ascii=False, indent=2))
        (root/(name+"-writing-calls.json")).write_text(json.dumps(writing_calls, ensure_ascii=False, indent=2))
        (root/(name+"-factual-calls.json")).write_text(json.dumps(factual_calls, ensure_ascii=False, indent=2))
        result["cases"].append(entry)
        (root/"acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        if not entry["passed"]:
            break
    result = finalize_acceptance(result, selected)
    (root/"acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
