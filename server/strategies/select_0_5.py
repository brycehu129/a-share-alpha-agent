"""select-0.5 的策略适配层：不改动任何打分逻辑，只把 shortterm_model 里已经在跑的
突破/回调两条 track 包成 server/strategies 要求的统一接口形状，好让它和以后加入的
其它策略（比如 reversal_0_1）在同一个循环里被对待。

这是"迁移现有策略进骨架"的第一步——本模块之外，shortterm_model.py 的实现一行没动。
"""
from shortterm_model import (SELECTION_VERSION, TARGET as _TARGET, ARCHIVE_SIZE as _ARCHIVE_SIZE,
                              SHORT_POLICY, screen_short, select_candidates, walk_forward_short)

STRATEGY_ID = SELECTION_VERSION
NAME = '突破/回调（select-0.5）'
POLICY = SHORT_POLICY
TARGET = _TARGET
ARCHIVE_SIZE = _ARCHIVE_SIZE
TRADABLE = True


def screen(world, tuning, hotmoney):
    """world: {'stocks','series','benchmark','cutoff','mid'}，mid 是共享的 alpha_model.screen()
    结果（多策略并行时只算一次，见 screen_short 的 mid 参数）。"""
    return screen_short(world['stocks'], world['series'], world['benchmark'], world['cutoff'],
                         tuning, hotmoney, mid=world.get('mid'))


def rank(screened, max_n):
    return select_candidates(screened, max_n)


def track_of(candidate):
    return candidate['strategy_type']


def calibrate(world, tuning, hotmoney_loader):
    return walk_forward_short(world['stocks'], world['series'], world['benchmark'], world['cutoff'],
                               tuning, hotmoney_loader=hotmoney_loader)
