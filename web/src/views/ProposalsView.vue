<script setup>
import { reactive } from 'vue'
import { get, post } from '../api'
import { useLoad } from '../composables/useLoad'
import { useAction } from '../composables/useAction'

const { data, loading, error, reload } = useLoad(() => get('/api/proposals'))
const notes = reactive({}) // 每个提议各自的备注输入
const act = useAction()

async function decide(p, kind) {
  const approve = kind === 'approve'
  try {
    await ElMessageBox.confirm(
      approve
        ? `批准后，${p.track}·${p.label} 将由 ${p.old} 改为 ${p.new}，从下一次盘前选股起新冻结的计划生效。`
        : `驳回提议 ${p.id}？`,
      approve ? '批准提议' : '驳回提议',
      { type: approve ? 'warning' : 'info', confirmButtonText: approve ? '批准' : '驳回', cancelButtonText: '取消' },
    )
  } catch (e) {
    return
  }
  const r = await act.run(() => post(`/api/proposals/${kind}`, { id: p.id, note: notes[p.id] || '' }))
  if (r) reload({ silent: true })
}
</script>

<template>
  <div>
    <div class="page-head"><h1>策略参数提议</h1></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" @click="reload()">重试</el-button>
    </el-alert>
    <el-alert v-if="act.error.value" :title="act.error.value" type="error" show-icon :closable="false" style="margin-bottom: 16px" />

    <div v-loading="loading && !data" class="stack" style="min-height: 200px">
      <template v-if="data">
        <p class="muted" style="margin: 0">
          AI 或规则只能在这里<b>提议</b>；你批准后才生效。风控红线（本金、持仓数、单只上限、单笔风险、回撤线、成本假设）不可提议，代码直接拒收。
          批准后从下一次盘前选股（工作日 08:40）起新冻结的计划用新规格，执行版本变为 {{ data.next_exec_version }}；已冻结的计划和已有验收记录不变，新旧样本分开统计。
        </p>

        <el-card shadow="never">
          <template #header>
            <div class="card-title"><span>当前生效规格</span><span class="sub">修订 {{ data.revision }}（{{ data.exec_version }}）</span></div>
          </template>
          <el-table :data="data.spec_rows" size="small">
            <el-table-column prop="track" label="track" width="100" />
            <el-table-column prop="label" label="参数" min-width="160" />
            <el-table-column label="当前值" min-width="160">
              <template #default="{ row }">
                <b class="num">{{ row.value }}</b><span v-if="row.changed" class="muted">（原 {{ row.base }}）</span>
              </template>
            </el-table-column>
            <el-table-column prop="range" label="允许范围" min-width="140"><template #default="{ row }"><span class="num muted">{{ row.range }}</span></template></el-table-column>
            <el-table-column prop="step" label="单次步长" width="120"><template #default="{ row }"><span class="num muted">{{ row.step }}</span></template></el-table-column>
          </el-table>
          <p class="muted">
            盈亏平衡胜率（按典型 ATR {{ data.nominal_atr_pct.toFixed(1) }}% 估算，每只股票的实际止损/止盈随自己的 ATR 而变）：
            <template v-for="(b, i) in data.breakevens" :key="b.track">{{ i ? ' · ' : '' }}{{ b.track }} <b class="num">{{ b.value.toFixed(1) }}%</b></template>
          </p>
        </el-card>

        <div class="card-title" style="margin-top: 4px"><span>待确认（{{ data.pending.length }}）</span></div>
        <el-card v-if="!data.pending.length" shadow="never">
          <span class="muted">没有待确认的提议。参数样本要满足 n≥30、日期组≥15 才有资格被提议；前瞻验收要等计划冻结后走完最长持有期，预计数周后才会攒够。</span>
        </el-card>
        <el-card v-for="p in data.pending" :key="p.id" shadow="never">
          <p style="margin: 0 0 6px">
            <b>{{ p.id }}</b> · {{ p.track }} · {{ p.label }}：<b>{{ p.old }} → {{ p.new }}</b>
            <span class="muted">（{{ p.source }}，{{ p.created_at }} 提交）</span>
          </p>
          <p class="muted" style="margin: 0 0 6px">盈亏平衡胜率 {{ p.breakeven_before }}% → {{ p.breakeven_after }}%（按典型 ATR 估算；越低，需要的胜率越低）</p>
          <p style="margin: 0 0 6px">提议方的理由（<em>未经验证的陈述，不是结论</em>）：{{ p.rationale || '（未填）' }}</p>
          <p class="muted" style="margin: 0">证据：n={{ p.evidence_n }}，日期组={{ p.evidence_cohorts }}，来自执行版本 {{ p.evidence_version }}。{{ p.evidence_summary }}</p>
          <div class="actions">
            <el-input v-model="notes[p.id]" placeholder="备注（可选）" style="max-width: 280px" />
            <el-button type="primary" :loading="act.loading.value" @click="decide(p, 'approve')">批准</el-button>
            <el-button type="danger" plain :loading="act.loading.value" @click="decide(p, 'reject')">驳回</el-button>
          </div>
        </el-card>

        <el-card shadow="never">
          <template #header><div class="card-title"><span>处理记录</span></div></template>
          <el-table :data="data.history" size="small" empty-text="暂无">
            <el-table-column prop="id" label="编号" width="130" />
            <el-table-column prop="status" label="状态" width="110" />
            <el-table-column prop="target" label="参数" min-width="170" />
            <el-table-column prop="change" label="改动" min-width="120"><template #default="{ row }"><span class="num">{{ row.change }}</span></template></el-table-column>
            <el-table-column prop="source" label="来源" width="90" />
            <el-table-column prop="note" label="说明" min-width="160"><template #default="{ row }"><span class="muted">{{ row.note }}</span></template></el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never">
          <template #header><div class="card-title"><span>已批准的修订</span></div></template>
          <el-table :data="data.revisions" size="small" empty-text="尚无修订：使用 exec-0.3 原始规格">
            <el-table-column prop="revision" label="修订" width="80" />
            <el-table-column prop="at" label="时间" width="150" />
            <el-table-column prop="target" label="参数" min-width="170" />
            <el-table-column prop="change" label="改动" min-width="120"><template #default="{ row }"><span class="num">{{ row.change }}</span></template></el-table-column>
            <el-table-column prop="note" label="备注" min-width="160"><template #default="{ row }"><span class="muted">{{ row.note }}</span></template></el-table-column>
          </el-table>
        </el-card>
      </template>
    </div>
  </div>
</template>
