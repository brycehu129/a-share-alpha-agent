<script setup>
import { computed, ref } from 'vue'
import { get, post } from '../../api'
import { useAction } from '../../composables/useAction'
import { fmtNum } from '../../format'
import RiseFall from '../RiseFall.vue'

// 加入自选：输入代码 / 名称 / 拼音首字母，下拉里选一只，名称、现价、涨跌停价自动带出来（和券商 App 的「添加自选」一样）。
// 选中后先展示这只股票的行情，再点「加入自选」——不会因为手滑选错就直接写进账本。
const emit = defineEmits(['added'])

const query = ref('')
const picked = ref(null) // /api/book/lookup 的结果
const note = ref('')
const looking = ref(false)
const lookupError = ref('')
const adding = useAction()
let lastSuggestions = []
let seq = 0

async function fetchSuggestions(q, cb) {
  const text = q.trim()
  if (!text) {
    lastSuggestions = []
    return cb([])
  }
  try {
    const r = await get('/api/book/search?q=' + encodeURIComponent(text))
    lastSuggestions = r.results.map((x) => ({ ...x, value: x.code }))
  } catch (e) {
    lastSuggestions = []
    lookupError.value = e.message
  }
  cb(lastSuggestions)
}

async function lookup(symbol) {
  const mine = ++seq
  looking.value = true
  lookupError.value = ''
  picked.value = null
  try {
    const r = await get('/api/book/lookup?symbol=' + encodeURIComponent(symbol))
    if (mine === seq) picked.value = r
  } catch (e) {
    if (mine === seq) lookupError.value = e.message
  } finally {
    if (mine === seq) looking.value = false
  }
}

// 从下拉里选中；或者在没有高亮项时按回车（select-when-unmatched）：6 位代码直接查，其余取第一条候选。
function onSelect(item) {
  if (item.symbol) return lookup(item.symbol)
  const text = (item.value || '').trim()
  if (/^\d{6}(\.(sh|sz))?$/i.test(text) || /^(sh|sz)\d{6}$/i.test(text)) return lookup(text)
  if (lastSuggestions.length) return lookup(lastSuggestions[0].symbol)
  lookupError.value = '没有找到匹配的沪深A股，请检查代码或名称。'
}

function clearPick() {
  seq++
  picked.value = null
  lookupError.value = ''
  query.value = ''
  note.value = ''
}

async function add() {
  const p = picked.value
  const r = await adding.run(() => post('/api/book/watch', { symbol: p.symbol, name: p.name, note: note.value }))
  if (r) {
    clearPick()
    emit('added')
  }
}

const limitText = computed(() => (picked.value && picked.value.limit_up ? `涨停 ${fmtNum(picked.value.limit_up)} / 跌停 ${fmtNum(picked.value.limit_down)}` : ''))
</script>

<template>
  <div class="picker">
    <el-autocomplete
      v-model="query"
      :fetch-suggestions="fetchSuggestions"
      :trigger-on-focus="false"
      :debounce="250"
      :select-when-unmatched="true"
      clearable
      size="large"
      placeholder="输入股票代码、名称或拼音首字母，如 600519 / 茅台 / gzmt"
      class="picker-input"
      @select="onSelect"
      @clear="clearPick"
    >
      <template #default="{ item }">
        <span class="hit-name">{{ item.name }}</span>
        <span class="hit-code num">{{ item.code }}</span>
        <span class="hit-meta">{{ item.pinyin }}<template v-if="item.board"> · {{ item.board }}</template></span>
      </template>
    </el-autocomplete>

    <el-alert v-if="lookupError" :title="lookupError" type="warning" show-icon :closable="false" class="picker-msg" />
    <div v-loading="looking" class="preview" :class="{ empty: !picked }" v-if="picked || looking">
      <template v-if="picked">
        <div class="preview-main">
          <div>
            <span class="stock-name preview-name">{{ picked.name || picked.symbol }}</span>
            <span class="num preview-code">{{ picked.symbol }}</span>
          </div>
          <div class="preview-price">
            <span class="num price">{{ fmtNum(picked.last) }}</span>
            <RiseFall :value="picked.change_pct" />
          </div>
          <span class="muted">{{ limitText }}</span>
        </div>
        <el-alert v-if="picked.warning" :title="picked.warning" type="info" show-icon :closable="false" />
        <div class="preview-actions">
          <el-input v-model="note" placeholder="备注（可选）" maxlength="200" class="note" />
          <el-button v-if="picked.in_watch" disabled>已在自选中</el-button>
          <el-button v-else type="primary" :loading="adding.loading.value" @click="add">加入自选</el-button>
          <el-button @click="clearPick">取消</el-button>
        </div>
        <span v-if="picked.held_shares" class="muted">这只股票你已持有 {{ picked.held_shares }} 股。</span>
        <el-alert v-if="adding.error.value" :title="adding.error.value" type="error" show-icon :closable="false" />
      </template>
    </div>
  </div>
</template>

<style scoped>
.picker { display: flex; flex-direction: column; gap: 10px; }
.picker-input { width: 100%; max-width: 560px; }
.picker-msg { max-width: 560px; }
.hit-name { font-weight: 600; margin-right: 10px; }
.hit-code { color: var(--as-muted); margin-right: 10px; }
.hit-meta { color: var(--as-muted); font-size: 12px; }
.preview { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; max-width: 560px; min-height: 60px; }
.preview-main { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; }
.preview-name { font-size: 16px; }
.preview-code { color: var(--as-muted); margin-left: 8px; }
.preview-price { display: flex; align-items: baseline; gap: 10px; }
.price { font-size: 20px; font-weight: 700; }
.preview-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.note { flex: 1; min-width: 160px; }
</style>
