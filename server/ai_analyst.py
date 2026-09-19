"""盘后 AI 研判：把 live_check 算出来的事实交给 Claude 做定性解读。

边界（也写进了 system prompt，改这里要同步改 AI_LAYER.md）：

- 模型**只能**基于传进去的结构化事实说话。本期还没有接新闻和公告，所以明确禁止
  它提及任何消息面、政策、研报、业绩——它没有这些数据，讲出来就是编的。这是
  金融场景里代价最高的一类幻觉，必须在提示词层面堵死。
- 模型的结论是**研究假设，不是已验证的胜率**。本项目已有的规则策略花了很大力气
  只报有样本支撑的频率，AI 层继承同一条纪律。
- 输出 schema 固定，每条判断带 confidence，全部留档——等积累够了才能回头算
  AI 研判的真实命中率（这也是后面复盘闭环的数据基础）。
"""
import json

from shortterm_model import BREAKOUT, PULLBACK, SHORT_POLICY, SELECTION_VERSION as STRATEGY_VERSION

PROMPT_VERSION = 'postclose-analyst-1'

CANDIDATE_VERDICTS = ['buy_tomorrow', 'watch', 'pass']
HOLDING_VERDICTS = ['hold', 'add', 'reduce', 'exit', 'swing_t']

# 结构化输出只接受 JSON Schema 的一个子集：type / enum / description / title /
# properties / required / additionalProperties / items / minItems(仅0或1) /
# $ref / $defs / anyOf / allOf。`minimum`、`maximum`、`maxItems` 这些**不在其中**
# ——官方 SDK 的 parse() 辅助会把它们从 schema 里摘掉、折进 description 文本。
# 我们直接透传原始 schema（不走 parse()，那需要额外定义 pydantic 模型），所以
# 必须自己守住这个子集，否则首次真实调用就会被拒。数值边界写进 description，
# 模型照样看得见，只是不再由 schema 强制。
# 有测试 test_schema_uses_only_supported_keywords 守着这条约束。
SUPPORTED_KEYWORDS = {'type', 'enum', 'description', 'title', 'properties',
                      'required', 'additionalProperties', 'items', 'minItems',
                      '$ref', '$defs', 'anyOf', 'allOf'}

_LEVELS = {
    'type': 'object',
    'properties': {
        'entry_zone': {'type': 'string', 'description': '建议关注的买入价格区间，或"不建议买入"'},
        'stop': {'type': 'string', 'description': '止损参考价位及依据'},
        'target': {'type': 'string', 'description': '目标参考价位及依据'},
    },
    'required': ['entry_zone', 'stop', 'target'], 'additionalProperties': False,
}


def _judgement(verdicts, verdict_desc):
    return {
        'type': 'object',
        'properties': {
            'symbol': {'type': 'string'},
            'name': {'type': 'string'},
            'verdict': {'type': 'string', 'enum': verdicts, 'description': verdict_desc},
            'confidence': {'type': 'integer',
                           'description': '取值只能是 1 到 5 的整数。1=证据很弱，5=给定事实高度一致；'
                                          '这是对判断依据强度的自评，不是胜率'},
            'reason': {'type': 'string', 'description': '结论依据，必须引用输入里的具体数值'},
            'risks': {'type': 'string', 'description': '这个判断可能错在哪里，以及什么信号会推翻它'},
            'key_levels': _LEVELS,
        },
        'required': ['symbol', 'name', 'verdict', 'confidence', 'reason', 'risks', 'key_levels'],
        'additionalProperties': False,
    }


SCHEMA = {
    'type': 'object',
    'properties': {
        'market': {
            'type': 'object',
            'properties': {
                'tone': {'type': 'string', 'enum': ['risk_on', 'neutral', 'risk_off']},
                'summary': {'type': 'string', 'description': '当天A股整体情况，150字以内'},
                'key_points': {'type': 'array', 'items': {'type': 'string'},
                               'description': '最多6条'},
                'tomorrow_watch': {'type': 'array', 'items': {'type': 'string'},
                                   'description': '明天需要关注的具体观察点，最多5条'},
            },
            'required': ['tone', 'summary', 'key_points', 'tomorrow_watch'],
            'additionalProperties': False,
        },
        'candidates': {'type': 'array', 'items': _judgement(
            CANDIDATE_VERDICTS, 'buy_tomorrow=明日开盘可考虑按计划买入；watch=继续观察不动手；pass=这轮放弃')},
        'holdings': {'type': 'array', 'items': _judgement(
            HOLDING_VERDICTS, 'hold=继续持有；add=可考虑加仓；reduce=减仓；exit=清仓；swing_t=适合做T')},
        'data_caveats': {'type': 'array', 'items': {'type': 'string'},
                         'description': '本次输入里缺失或可疑的数据，以及它们如何限制上面的判断；最多8条'},
    },
    'required': ['market', 'candidates', 'holdings', 'data_caveats'],
    'additionalProperties': False,
}


def system_prompt():
    """策略口径直接写进提示词，让模型的研判和规则层对齐，而不是自由发挥。"""
    return f"""你是一个A股盘后分析助手，服务于一套已经在运行的量化研究系统。

# 这套系统的策略口径（版本 {STRATEGY_VERSION}）

候选股来自两条互相独立、分别校准的规则track，都只在**已收盘**的日线上筛选：

- **突破(breakout)**：行业强度前10%；20日超额不超过{BREAKOUT['excess20_max']}pp（否则不算新鲜突破）；
  近3日涨幅≥{BREAKOUT['return3_min']}%；当日首次放量（今日量比≥{BREAKOUT['volume_ratio_5d_min']}且昨日未放量）；
  接近20日新高；MA5偏离在0–{BREAKOUT['ma5_deviation_max']}%。
- **回调反弹(pullback)**：行业强度前10%；20日超额为正（已确认中期强势）；近2日回调≤{PULLBACK['return2_max']}%；
  回调缩量（量比≤{PULLBACK['volume_ratio_20d_max']}）；MA20偏离在0–{PULLBACK['deviation_max']}%；当日收阳确认。

持仓规则：最多{SHORT_POLICY['max_positions']}只，单只最多{SHORT_POLICY['max_weight']*100:.0f}%仓位，
止损{SHORT_POLICY['stop_pct']*100:.0f}%、止盈{SHORT_POLICY['target_pct']*100:.0f}%、
最长持有{SHORT_POLICY['hold_sessions']}个交易日。买入只在次日09:30–09:35窗口按冻结计划的
±{SHORT_POLICY['entry_gap_max']*100:.0f}%入场带执行，错过就跳过不补买。

候选的"综合分"是规则打分，**不是胜率**。附带的概率如果标注为"有回看偏差的历史研究"，
说明它来自用当前名单回看历史的统计，存在幸存者偏差，不能当作真实胜率。

# 你的任务

基于用户消息里的结构化事实，给出：当天大盘情况、候选池逐只研判、持仓逐只研判。

# 硬性要求

1. **只能依据输入里的数据说话。** 本次输入**不包含任何新闻、公告、研报、业绩、政策
   信息**。因此禁止提及或暗示任何消息面内容——不要写"受政策利好""消息面平静"
   "业绩预期向好"之类的话。你没有这些数据，写出来就是编造。
2. 每条结论的 reason 必须引用输入里的**具体数值**（价格、偏离、量比、涨跌幅、分数等），
   不许只给方向性形容词。
3. 数据缺失、过期或自相矛盾时，在 data_caveats 里明确说出来，并相应降低 confidence，
   不要用推测填补空缺。
4. confidence 是你对"依据强度"的自评，**不是胜率、不是概率**。不要输出任何百分比形式
   的胜率数字。
5. 候选池里每一只都要给判断；持仓里每一只都要给判断。symbol 必须原样照抄输入里的代码。
6. 注意区分：输入里的 `as_of_features` 是策略在上一个收盘日算的特征，
   `price_facts`/`live_gates` 是用最新价重算的。两者不一致时要指出来——那通常正是
   "候选选出来之后价格又走了一段"的信号。
7. 用中文回答，措辞克制。这是研究参考，不是投资建议；不要用"必涨""稳了"这类表述。"""


def build_payload(check_result, context, holdings, watchlist):
    """组装给模型的结构化输入。只传它判断得上的字段，不倒整个归档。"""
    rows = []
    for row in check_result['rows']:
        item = {k: row[k] for k in ('symbol', 'name', 'roles', 'quote', 'price_facts', 'limit_facts')}
        if row.get('strategy'):
            item['strategy'] = row['strategy']
        if row.get('live_gates'):
            item['live_gates'] = {k: {'passed': v[0], 'detail': v[1]}
                                  for k, v in row['live_gates'].items()}
        for key in ('plan', 'holding', 'watch'):
            if row.get(key):
                item[key] = row[key]
        if row['issues']:
            item['issues'] = row['issues']
        rows.append(item)
    return {
        'market_context': context,
        'strategy_context': check_result['strategy_context'],
        'stocks': rows,
        'counts': {'holdings': len(holdings), 'watchlist': len(watchlist),
                   'candidates': sum('candidate' in r['roles'] for r in check_result['rows'])},
        'rule_layer_caveats': check_result['caveats'],
    }


def _index(rows):
    return {r.get('symbol'): r for r in rows if isinstance(r, dict)}


def validate(data, check_result):
    """核对模型的输出和我们实际送进去的股票对得上。

    模型可能漏掉某只、也可能返回一只根本没传给它的代码。两种都不当成致命错误——
    规则层的结论仍然有效——但必须显式标出来，不能让一只票在报告里静默消失。"""
    expected_holdings = {r['symbol'] for r in check_result['rows'] if 'holding' in r['roles']}
    expected_candidates = {r['symbol'] for r in check_result['rows'] if 'candidate' in r['roles']}
    issues = []
    for key, expected in (('candidates', expected_candidates), ('holdings', expected_holdings)):
        returned = _index(data.get(key, []))
        missing = sorted(expected - set(returned))
        extra = sorted(set(returned) - expected)
        if missing:
            issues.append('模型未对以下%s给出判断：%s' % (key, ','.join(missing)))
        if extra:
            issues.append('模型返回了未送入的%s代码，已剔除：%s' % (key, ','.join(extra)))
            data[key] = [r for r in data[key] if r.get('symbol') in expected]
    return issues


def analyze(check_result, context, holdings, watchlist):
    """返回 (研判结果或None, 元信息)。调用失败不抛出——盘后报告要能降级成纯规则版。"""
    import claude_client
    payload = build_payload(check_result, context, holdings, watchlist)
    user_content = ('以下是今天的结构化事实，请按 schema 给出研判。\n\n'
                    + json.dumps(payload, ensure_ascii=False, indent=1))
    meta = {'prompt_version': PROMPT_VERSION, 'strategy_version': STRATEGY_VERSION,
            'payload_stocks': len(payload['stocks'])}
    try:
        data, call_meta = claude_client.complete_json(system_prompt(), user_content, SCHEMA)
    except claude_client.ClaudeError as exc:
        meta.update(status=exc.status, error=exc.message)
        return None, meta
    meta.update(call_meta, status='ok', validation_issues=validate(data, check_result))
    return data, meta
