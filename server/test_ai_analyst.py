import json
import unittest
from unittest.mock import patch

import ai_analyst
import claude_client


def check_result(symbols_with_roles):
    return {'rows': [{'symbol': s, 'name': s, 'roles': roles,
                      'quote': {'last': '10', 'change_pct': '1'}, 'price_facts': {},
                      'limit_facts': {}, 'issues': []}
                     for s, roles in symbols_with_roles],
            'strategy_context': {'cutoff': '2026-09-18'}, 'caveats': []}


class SchemaTests(unittest.TestCase):
    def test_verdicts_are_constrained_to_the_whitelist(self):
        """自由文本的结论没法被后面的持仓监控自动执行，所以枚举必须钉死。"""
        candidate = ai_analyst.SCHEMA['properties']['candidates']['items']
        holding = ai_analyst.SCHEMA['properties']['holdings']['items']
        self.assertEqual(candidate['properties']['verdict']['enum'], ai_analyst.CANDIDATE_VERDICTS)
        self.assertEqual(holding['properties']['verdict']['enum'], ai_analyst.HOLDING_VERDICTS)

    def test_every_object_forbids_extra_properties(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    self.assertIs(node.get('additionalProperties'), False)
                    self.assertTrue(node.get('required'))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(ai_analyst.SCHEMA)

    def test_confidence_bounds_live_in_the_description(self):
        """API 的结构化输出不支持 minimum/maximum，边界只能写进 description。"""
        conf = ai_analyst.SCHEMA['properties']['candidates']['items']['properties']['confidence']
        self.assertEqual(conf['type'], 'integer')
        self.assertIn('1 到 5', conf['description'])

    def test_schema_uses_only_supported_keywords(self):
        """结构化输出只接受 JSON Schema 的一个子集；minimum/maximum/maxItems 这些
        会让首次真实调用直接被拒。官方 SDK 的 parse() 辅助就是靠摘掉它们、折进
        description 来绕开的，我们直接透传 schema，所以必须自己守住。"""
        seen = set()

        def walk(node):
            if isinstance(node, dict):
                seen.update(node)
                for key, value in node.items():
                    if key in ('properties', '$defs'):
                        for child in value.values():
                            walk(child)
                    elif key in ('items', 'anyOf', 'allOf'):
                        walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(ai_analyst.SCHEMA)
        self.assertEqual(seen - ai_analyst.SUPPORTED_KEYWORDS, set())


class PromptTests(unittest.TestCase):
    def test_prompt_forbids_inventing_news(self):
        """本期没有接新闻源，模型一旦谈消息面就一定是编的——
        这是金融场景里代价最高的幻觉，必须在提示词里堵死。"""
        prompt = ai_analyst.system_prompt()
        self.assertIn('不包含任何新闻、公告、研报、业绩', prompt)
        self.assertIn('禁止', prompt)

    def test_prompt_carries_the_actual_strategy_thresholds(self):
        from shortterm_model import BREAKOUT, SHORT_POLICY
        prompt = ai_analyst.system_prompt()
        self.assertIn(str(BREAKOUT['return3_min']), prompt)
        self.assertIn(str(SHORT_POLICY['hold_sessions']), prompt)

    def test_prompt_refuses_to_present_scores_as_win_rates(self):
        self.assertIn('不是胜率', ai_analyst.system_prompt())


class ValidationTests(unittest.TestCase):
    def test_missing_symbols_are_flagged_not_silently_dropped(self):
        check = check_result([('sh600000', ['candidate']), ('sh600001', ['candidate'])])
        data = {'candidates': [{'symbol': 'sh600000'}], 'holdings': []}
        issues = ai_analyst.validate(data, check)
        self.assertTrue(any('sh600001' in i for i in issues))

    def test_hallucinated_symbols_are_removed(self):
        check = check_result([('sh600000', ['candidate'])])
        data = {'candidates': [{'symbol': 'sh600000'}, {'symbol': 'sz000999'}], 'holdings': []}
        issues = ai_analyst.validate(data, check)
        self.assertEqual([c['symbol'] for c in data['candidates']], ['sh600000'])
        self.assertTrue(any('sz000999' in i for i in issues))

    def test_clean_output_produces_no_issues(self):
        check = check_result([('sh600000', ['candidate', 'holding'])])
        data = {'candidates': [{'symbol': 'sh600000'}], 'holdings': [{'symbol': 'sh600000'}]}
        self.assertEqual(ai_analyst.validate(data, check), [])


class AnalyzeTests(unittest.TestCase):
    def test_failure_degrades_instead_of_raising(self):
        """AI 挂了不能让整份盘后报告失败——规则层的结论本身就有用。"""
        check = check_result([('sh600000', ['candidate'])])
        with patch('claude_client.complete_json',
                   side_effect=claude_client.ClaudeError('rate_limited', '慢点')):
            data, meta = ai_analyst.analyze(check, {}, [], [])
        self.assertIsNone(data)
        self.assertEqual(meta['status'], 'rate_limited')
        self.assertEqual(meta['prompt_version'], ai_analyst.PROMPT_VERSION)

    def test_success_records_provenance(self):
        check = check_result([('sh600000', ['candidate'])])
        reply = ({'market': {}, 'candidates': [{'symbol': 'sh600000'}], 'holdings': [],
                  'data_caveats': []}, {'model': 'claude-opus-5', 'input_tokens': 10})
        with patch('claude_client.complete_json', return_value=reply):
            data, meta = ai_analyst.analyze(check, {}, [], [])
        self.assertEqual(meta['status'], 'ok')
        self.assertEqual(meta['model'], 'claude-opus-5')
        self.assertEqual(meta['strategy_version'], ai_analyst.STRATEGY_VERSION)

    def test_payload_is_json_serializable(self):
        check = check_result([('sh600000', ['candidate'])])
        payload = ai_analyst.build_payload(check, {'indices': []}, [], [])
        json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload['counts']['candidates'], 1)

    def test_live_gate_tuples_become_named_fields(self):
        check = check_result([('sh600000', ['candidate'])])
        check['rows'][0]['live_gates'] = {'MA5偏离': (False, '8.2% (要求0–6%)')}
        payload = ai_analyst.build_payload(check, {}, [], [])
        gate = payload['stocks'][0]['live_gates']['MA5偏离']
        self.assertEqual(gate, {'passed': False, 'detail': '8.2% (要求0–6%)'})


if __name__ == '__main__':
    unittest.main()
