"""自选股盘中情景研判：把规则层触发的事件和当时的事实交给 Claude，产出**可机检的情景**。

和 ai_analyst（盘后、候选池口径）彻底分开——那份 prompt 会把候选池的策略口径
（"最长持有3个交易日""止损3%止盈5%"）拿来衡量你自己买的票，而系统根本不知道你为什么买。
这里的 prompt 只描述你的买入成本，以及系统按波动率(ATR)从成本价算出的止损/止盈参考位和可卖老仓。

**不画预测路径，给带触发条件的情景。** 日内走势预测的准确率接近随机，而一条预测曲线的
说服力远超它的信息量——你会信它，而它驱动的是真金白银的决策。情景是可证伪的：
"若放量站上 X，看 Y；若跌破 Z 则判断失效"，你能自己判断触发了没有，收盘后系统也能
机械地判定它对不对。为此**所有价位字段必须是数值**，写成"站稳1260附近"就没法对账。

模型给出的价位不直接信任：`validate_entry` 用代码逐条校验（方向、相对现价的位置、
涨跌停区间、目标与失效价在触发价的哪一侧），不合格的情景丢弃并记录原因，不推送。
"""
import json
import os

from ai_analyst import SUPPORTED_KEYWORDS  # noqa: F401  同一套结构化输出关键字约束

PROMPT_VERSION = 'sentinel-scenario-2'
ANALYSIS_TIMEOUT_S = 100
# 推理 token 和可见输出共用这个预算（Anthropic 直连和 OpenRouter 都是）。情景本身只是几百字的结构化
# JSON，但推理可能吃掉几千 token；预算太小会出现"推理耗尽、可见内容为空、照样计费"。
MAX_TOKENS = 12000
HINTS = ['hold', 'add', 'reduce', 'wait', 't_sell_high', 't_buy_low']
HOLDING_ONLY_HINTS = ('hold', 'reduce', 't_sell_high', 't_buy_low')

_SCENARIO = {
    'type': 'object',
    'properties': {
        'label': {'type': 'string', 'description': '情景名，如"放量上攻"、"回踩不破"、"跌破失守"'},
        'direction': {'type': 'string', 'enum': ['up', 'down']},
        'trigger_price': {'type': 'number', 'description': '触发价。up 情景必须高于现价，down 情景必须低于现价；优先取输入 key_levels 里的价位'},
        'trigger_condition': {'type': 'string', 'description': '触发条件的文字说明，如"放量站上均价线并守住5分钟"'},
        'target_low': {'type': 'number', 'description': '触发后的目标区间下沿'},
        'target_high': {'type': 'number', 'description': '触发后的目标区间上沿，必须高于 target_low'},
        'invalidate_price': {'type': 'number', 'description': '失效价：up 情景须低于现价（也就低于触发价），down 情景须高于现价；到了这个价说明这个情景的判断错了'},
    },
    'required': ['label', 'direction', 'trigger_price', 'trigger_condition', 'target_low', 'target_high',
                 'invalidate_price'],
    'additionalProperties': False,
}

SCHEMA = {
    'type': 'object',
    'properties': {
        'stocks': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'symbol': {'type': 'string'},
                'current_read': {'type': 'string', 'description': '现状描述，必须引用输入里的具体数值，150字以内'},
                'scenarios': {'type': 'array', 'items': _SCENARIO,
                              'description': '2到3个情景，只针对今天收盘前，至少包含一个 up 和一个 down'},
                'watch_metrics': {'type': 'array', 'items': {'type': 'string'}, 'description': '需要盯的指标，最多3条'},
                'action_hint': {'type': 'string', 'enum': HINTS,
                                'description': 't_sell_high/t_buy_low 只有输入里 t_base_shares>0 时才能用；'
                                               'hold/reduce 只对持仓有意义'},
                'confidence': {'type': 'integer',
                               'description': '取值只能是 1 到 5 的整数。这是对判断依据强度的自评，不是胜率'},
                'caveats': {'type': 'array', 'items': {'type': 'string'}, 'description': '本条判断的局限，最多2条'},
            },
            'required': ['symbol', 'current_read', 'scenarios', 'watch_metrics', 'action_hint', 'confidence', 'caveats'],
            'additionalProperties': False,
        }},
        'data_caveats': {'type': 'array', 'items': {'type': 'string'}, 'description': '输入里缺失或可疑的数据，最多5条'},
    },
    'required': ['stocks', 'data_caveats'],
    'additionalProperties': False,
}


def system_prompt():
    return """你是一个A股盘中助手，服务于一位**用自己的真金白银做决策**的个人投资者。系统只提醒、不下单，
决定永远由用户自己做。你的任务：针对刚刚触发的提醒，给出今天收盘前可能出现的几种情景。

# 你不知道的事（不要假装知道）

- 你**不知道用户为什么买这只股票**，所以不要用任何"策略规则"去评价它。用户告诉你的只有买入成本价；
  输入里的 stop_price / target_price 是**系统按波动率(ATR)从成本价算出的参考位**（不是用户设的），
  t_base_shares 是系统按 T+1 算出的今天可卖老仓。没有这些字段就是没算出来，不要替他设。
- 你**没有任何新闻、公告、研报、业绩、政策信息**。禁止提及或暗示任何消息面内容——你没有这些数据，
  写出来就是编造。
- 你**无法预测日内走势**。不要画路径、不要说"预计收于X"。只给带触发条件的情景。

# 怎么写情景

1. 给 2–3 个情景，只针对**今天收盘前**，至少各有一个 up 和 down。
2. 每个情景必须带**数值**：触发价、目标区间、失效价。up 情景的触发价必须**高于现价**，down 情景
   必须**低于现价**（已经到达的价位不叫触发条件）；up 的失效价必须**低于现价**（否则情景一发布就已失效），
   down 的失效价必须**高于现价**；目标区间在触发价的延伸方向上。
3. 价位**优先取输入 key_levels 里的值**（均线、均价线、日内高低、20日高低点、昨收、成本、止损、目标、
   涨跌停价），不要凭空造一个整数关口。所有价位必须在涨跌停区间内。
4. current_read 必须引用输入里的具体数值；对分时均价线（vwap）、日内区间位置、量比的解读要具体。
5. 输入里有 triggers（刚触发的规则）——先解释这个触发在当前位置意味着什么，再给情景。
6. action_hint：t_sell_high / t_buy_low 只有在输入里 holding.t_base_shares>0 时才能用（A股 T+1，
   只能用底仓先卖后买）；不是持仓的股票只能用 wait 或 add。拿不准就 wait。
7. confidence 是你对"依据强度"的自评（1–5），**不是胜率、不是概率**，不要输出百分比胜率。
8. 数据缺失或自相矛盾时写进 data_caveats，并降低 confidence，不要推测填补。
9. 中文，措辞克制。这是研究参考，不构成投资建议；不要用"必涨""稳了"这类表述。"""


def key_levels(quote, facts, day, holding=None, limits=None):
    """给模型挑价位用的候选清单（名字 → 价格）。让它从这里选而不是凭空造数字。"""
    def f(v):
        return float(v) if v not in (None, '') else None
    levels = {'昨收': f(quote.get('previous_close')), '今开': f(quote.get('open')),
              '日内最高': day.get('day_high'), '日内最低': day.get('day_low'), '分时均价线': day.get('vwap'),
              'MA5': facts.get('ma5'), 'MA20': facts.get('ma20'), 'MA60': facts.get('ma60'),
              '20日最高收盘': facts.get('high20_close'), '20日最低收盘': facts.get('low20_close'),
              '涨停价': (limits or {}).get('limit_up'), '跌停价': (limits or {}).get('limit_down')}
    if holding:
        levels.update({'成本价': f(holding.get('cost_price')), '系统止损位': f(holding.get('stop_price')),
                       '系统止盈位': f(holding.get('target_price'))})
    return {k: round(v, 4) for k, v in levels.items() if v}


def build_payload(entries, market=None):
    """entries: [{'symbol','name','roles','triggers','quote','price_facts','day','limits','holding','watch',
    'key_levels','recent'}]。"""
    return {'stocks': entries, 'market': market or {},
            'note': '价位单位为元；quote 为最新一次观测到的报价；recent 是最近若干分钟的分钟收盘价。'}


# --- 校验：不信任模型给的价位 -------------------------------------------------

def validate_scenario(sc, last, limit_up=None, limit_down=None):
    """返回 (是否合格, 原因)。"""
    nums = {k: sc.get(k) for k in ('trigger_price', 'target_low', 'target_high', 'invalidate_price')}
    for k, v in nums.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not v > 0 or v != v or v in (float('inf'),):
            return False, '%s 不是有效的正数' % k
    trig, lo, hi, inv = (nums[k] for k in ('trigger_price', 'target_low', 'target_high', 'invalidate_price'))
    if sc.get('direction') not in ('up', 'down'):
        return False, 'direction 只能是 up/down'
    if not lo < hi:
        return False, '目标区间下沿必须低于上沿'
    if limit_up and limit_down:
        for k, v in nums.items():
            if not limit_down * 0.999 <= v <= limit_up * 1.001:
                return False, '%s=%s 超出今日涨跌停区间 [%s, %s]，不可能到达' % (k, v, limit_down, limit_up)
    if sc['direction'] == 'up':
        if not trig > last:
            return False, 'up 情景的触发价 %s 没有高于现价 %s（已经到达的价位不是触发条件）' % (trig, last)
        if not inv < trig:
            return False, 'up 情景的失效价必须低于触发价'
        if not inv < last:
            # 失效价不在现价下方，说明这个情景在发布那一刻就已经失效了——自相矛盾，
            # 推给用户只会是一条一出生就作废的提示。
            return False, 'up 情景的失效价 %s 不低于现价 %s（发布时就已失效）' % (inv, last)
        if not lo >= trig:
            return False, 'up 情景的目标区间必须在触发价之上'
    else:
        if not trig < last:
            return False, 'down 情景的触发价 %s 没有低于现价 %s' % (trig, last)
        if not inv > trig:
            return False, 'down 情景的失效价必须高于触发价'
        if not inv > last:
            return False, 'down 情景的失效价 %s 不高于现价 %s（发布时就已失效）' % (inv, last)
        if not hi <= trig:
            return False, 'down 情景的目标区间必须在触发价之下'
    return True, None


def validate_entry(entry, last, limit_up=None, limit_down=None, is_holding=False, t_base=0):
    """就地清洗一条个股研判。返回 (清洗后的 entry, 问题列表)。"""
    problems, kept = [], []
    for sc in entry.get('scenarios') or []:
        ok, why = validate_scenario(sc, last, limit_up, limit_down)
        if ok:
            kept.append(sc)
        else:
            problems.append('情景「%s」被丢弃：%s' % (sc.get('label'), why))
    entry['scenarios'] = kept
    if not kept:
        problems.append('没有任何合格的情景，本条只展示规则层事实')
    hint = entry.get('action_hint')
    if hint in ('t_sell_high', 't_buy_low') and not t_base:
        problems.append('action_hint=%s 需要今天有可卖的老仓做底仓，已改为 wait' % hint)
        entry['action_hint'] = 'wait'
    elif hint in HOLDING_ONLY_HINTS and not is_holding:
        problems.append('action_hint=%s 只对持仓有意义，已改为 wait' % hint)
        entry['action_hint'] = 'wait'
    conf = entry.get('confidence')
    if not isinstance(conf, int) or isinstance(conf, bool) or not 1 <= conf <= 5:
        problems.append('confidence 不在 1–5，已按 1 处理')
        entry['confidence'] = 1
    return entry, problems


def analyze(payload):
    """返回 (研判或 None, 元信息)。失败不抛出——哨兵要能降级成只推规则层结论。"""
    import claude_client
    from shortterm_model import SELECTION_VERSION  # noqa: F401
    meta = {'prompt_version': PROMPT_VERSION, 'stocks': len(payload['stocks'])}
    user = '以下是刚触发提醒的股票及当时的事实，请按 schema 给出情景。\n\n' + json.dumps(payload, ensure_ascii=False)
    try:
        # 由独立的分析服务调用，systemd 时限 150 秒：单次最多等 100 秒且不重试，总耗时才有上界。
        # SENTINEL_MODEL：哨兵一天最多 15 次调用，可以用比盘后报告更便宜的模型。模型 id 必须符合当前
        # 后端的写法（OpenRouter 用 `anthropic/claude-sonnet-5`，直连用 `claude-sonnet-5`）。
        data, call_meta = claude_client.complete_json(system_prompt(), user, SCHEMA, effort='medium',
                                                      model=os.environ.get('SENTINEL_MODEL') or None,
                                                      max_tokens=MAX_TOKENS, timeout=ANALYSIS_TIMEOUT_S, max_retries=0)
    except claude_client.ClaudeError as exc:
        meta.update(status=exc.status, error=exc.message)
        return None, meta
    meta.update(call_meta, status='ok')
    return data, meta
