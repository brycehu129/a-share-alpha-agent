"""多策略注册表：哪些策略存在、哪个是主力，唯一权威来源在这里。

策略 = 一个 Python 模块，不是网页可编辑的配置。这个项目里所有记录按版本号冻结、
不可改写；如果策略定义能在网页上改，"某个 selection_version 当时到底对应哪套规则"
就成了一个活的、追不回去的问题。REGISTRY 显式列出、可 grep、可 diff，和
proposals.py 里"白名单由代码强制"是同一个思路——目录扫描的话，一个放错位置的
文件就会悄悄铸造一个新版本号并冻结出无法回收的记录。

每个策略模块必须导出：
  STRATEGY_ID   str，同时就是这个策略产生的 selection_version
  NAME          str，人看的名字
  POLICY        dict，仓位/风控参数的形状（仅供 plan_levels()/sizing() 展示用；
                真实风控红线是账户层面的常量，不因策略而异）
  TARGET        str，验收口径说明文字，写进 forecast 记录
  ARCHIVE_SIZE  int，每个截止日留档多少条
  TRADABLE      bool，False 的策略永远只留档、不进约束账户（比如反转策略）
  screen(world, tuning, hotmoney) -> screened
  rank(screened, max_n) -> [candidate]
  track_of(candidate) -> str，喂给 exec_spec.build_spec 的 track 名

可选：
  calibrate(world, tuning, hotmoney_loader) -> {...} | None
"""
from tushare_sync import read, save

from . import reversal_0_1, select_0_5

REGISTRY = {
    select_0_5.STRATEGY_ID: select_0_5,
    reversal_0_1.STRATEGY_ID: reversal_0_1,
}

MAIN_STRATEGY = select_0_5.STRATEGY_ID   # 目前唯一进约束账户的策略
MAX_ENABLED = 3

_REQUIRED_ATTRS = ('STRATEGY_ID', 'NAME', 'POLICY', 'TARGET', 'ARCHIVE_SIZE', 'TRADABLE')
_REQUIRED_FUNCS = ('screen', 'rank', 'track_of')


def validate(module):
    """模块是不是一个合格的策略实现；返回问题列表（空 = 通过）。"""
    problems = [f'缺少 {name}' for name in _REQUIRED_ATTRS if not hasattr(module, name)]
    problems += [f'缺少可调用的 {name}' for name in _REQUIRED_FUNCS if not callable(getattr(module, name, None))]
    return problems


def _check_registry():
    for strategy_id, module in REGISTRY.items():
        if getattr(module, 'STRATEGY_ID', None) != strategy_id:
            raise AssertionError('REGISTRY 的 key 必须等于模块自己的 STRATEGY_ID: %r != %r' %
                                  (strategy_id, getattr(module, 'STRATEGY_ID', None)))
        problems = validate(module)
        if problems:
            raise AssertionError('策略模块 %s 不合格: %s' % (strategy_id, '; '.join(problems)))
    if MAIN_STRATEGY not in REGISTRY:
        raise AssertionError('MAIN_STRATEGY 不在 REGISTRY 里: ' + MAIN_STRATEGY)


_check_registry()   # 导入期就校验，坏了在这里炸，而不是等某个截止日跑到一半才发现。


def default_toggles():
    return {'enabled': [MAIN_STRATEGY], 'main': MAIN_STRATEGY}


def _toggles_path(history):
    return history / 'strategies' / 'active.json'


def load_toggles(history):
    """{'enabled': [策略id...], 'main': 策略id}。文件不存在、损坏、或写进了未注册的 id
    时一律回退默认（只跑主策略）——宁可少跑，不可悄悄跑一个不存在的策略槽位。"""
    path = _toggles_path(history)
    if not path.exists():
        return default_toggles()
    try:
        data = read(path)
    except ValueError:
        return default_toggles()
    enabled = [sid for sid in data.get('enabled', []) if sid in REGISTRY][:MAX_ENABLED]
    main = data.get('main')
    if main not in enabled:
        main = enabled[0] if enabled else MAIN_STRATEGY
    if not enabled:
        enabled = [main]
    return {'enabled': enabled, 'main': main}


def save_toggles(history, enabled, main):
    """校验后原子写入（tushare_sync.save：temp 文件 + Path.replace，Windows/Linux 都安全）。
    未知 id 直接拒收——错别字不该悄悄铸造一个空跑的策略槽位。"""
    unknown = [sid for sid in enabled if sid not in REGISTRY]
    if unknown:
        raise KeyError('未注册的策略: ' + ', '.join(unknown))
    if main not in enabled:
        raise ValueError('主力策略必须在启用列表里: ' + main)
    if len(enabled) > MAX_ENABLED:
        raise ValueError('最多同时启用 %d 个策略' % MAX_ENABLED)
    if not enabled:
        raise ValueError('至少要启用一个策略')
    save(_toggles_path(history), {'enabled': enabled, 'main': main})
