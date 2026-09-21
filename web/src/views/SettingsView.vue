<script setup>
import { reactive, watch } from 'vue'
import { get, post, downloadPost } from '../api'
import { useLoad } from '../composables/useLoad'
import { useAction } from '../composables/useAction'

const { data, loading, error, reload } = useLoad(() => get('/api/settings'))

const LEVEL_TEXT = { ok: '正常', warn: '注意', crit: '严重', skip: '未检查' }
const LEVEL_TAG = { ok: 'success', warn: 'warning', crit: 'danger', skip: 'info' }
const BACKUP_TAG = { ok: ['success', '正常'], warn: ['warning', '需要留意'], none: ['info', '尚未运行'] }

// 输入框只放「要提交的新值」；已保存的值只显示掩码，从不回填明文。
const form = reactive({ webhook_url: '', api_key: '', model: '', sentinel_model: '' })
watch(
  () => data.value && data.value.llm,
  (llm) => {
    if (!llm) return
    form.model = llm.model.page_value
    form.sentinel_model = llm.sentinel_model.page_value
  },
  { immediate: true },
)

const saveWebhook = useAction()
const testPush = useAction()
const saveKey = useAction()
const saveModels = useAction()
const clearKey = useAction()
const checkLlm = useAction()
const download = useAction()
const checkResult = reactive({ shown: false, ok: false, lines: [] })

async function onSaveWebhook() {
  const r = await saveWebhook.run(() => post('/api/settings/webhook', { webhook_url: form.webhook_url }))
  if (r) {
    data.value.push = r.push
    form.webhook_url = ''
  }
}
const onTestPush = () => testPush.run(() => post('/api/settings/test-push'))

async function onSaveKey() {
  const r = await saveKey.run(() => post('/api/llm/key', { api_key: form.api_key }))
  if (r) {
    data.value.llm = r.llm
    form.api_key = ''
  }
}
async function onSaveModels() {
  const r = await saveModels.run(() => post('/api/llm/models', { model: form.model, sentinel_model: form.sentinel_model }))
  if (r) data.value.llm = r.llm
}
async function onClearKey() {
  try {
    await ElMessageBox.confirm('清除后，页面保存的 key 会被删除（若环境变量里有 key 则回落到它）。', '清除 key', {
      type: 'warning',
      confirmButtonText: '清除',
      cancelButtonText: '取消',
    })
  } catch (e) {
    return
  }
  const r = await clearKey.run(() => post('/api/llm/clear'))
  if (r) data.value.llm = r.llm
}
async function onCheck() {
  checkResult.shown = false
  const r = await checkLlm.run(() => post('/api/llm/check'), false)
  if (r) Object.assign(checkResult, { shown: true, ok: r.check_ok, lines: r.lines })
}
const onDownload = () => download.run(() => downloadPost('/api/backup/download'), (name) => `已下载 ${name}`)

function healthHead(h) {
  if (h.heartbeat_age_min === null) return { tag: 'info', label: '尚未运行', text: '健康检查还没有运行过（部署后 5 分钟内第一次触发）。' }
  const age = Math.round(h.heartbeat_age_min)
  if (h.stale) {
    return { tag: 'warning', label: '已停止', text: `健康检查已 ${age} 分钟没有运行——没人在盯着系统了，请在服务器上看 systemctl status alpha-shadow-health.timer。` }
  }
  return { tag: h.critical ? 'danger' : 'success', label: h.critical ? '有严重问题' : '运行中', text: `最近一次检查 ${age} 分钟前。` }
}
const fmtAt = (iso) => String(iso || '').slice(0, 16).replace('T', ' ')
// 「上次执行」列：当天的只显示时分秒，其它天带上月日；没有依据的执行记录（例如今天不在运行时段）显示 —
const fmtWhen = (iso) => {
  const s = String(iso || '')
  if (!s) return '—'
  const t = s.slice(11, 19)
  return s.slice(0, 10) === new Date().toLocaleDateString('sv-SE') ? t : `${s.slice(5, 10)} ${t.slice(0, 5)}`
}
</script>

<template>
  <div>
    <div class="page-head"><h1>设置</h1></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-bottom: 16px">
      <el-button size="small" @click="reload()">重试</el-button>
    </el-alert>

    <div v-loading="loading && !data" class="stack" style="min-height: 200px">
      <template v-if="data">
        <!-- 推送 -->
        <el-card shadow="never">
          <template #header>
            <div class="card-title">
              <span>盘中推送 · 企业微信群机器人</span>
              <el-tag :type="data.push.configured ? 'success' : 'info'" round>
                {{ data.push.configured ? `已配置 ${data.push.masked}` : '尚未配置' }}
              </el-tag>
            </div>
          </template>
          <el-form label-position="top" @submit.prevent="onSaveWebhook">
            <el-form-item label="Webhook 地址">
              <el-input v-model="form.webhook_url" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." autocomplete="off" clearable />
            </el-form-item>
            <el-alert v-if="saveWebhook.error.value" :title="saveWebhook.error.value" type="error" show-icon :closable="false" />
            <el-alert v-if="testPush.error.value" :title="testPush.error.value" type="error" show-icon :closable="false" />
            <div class="actions">
              <el-button type="primary" native-type="submit" :loading="saveWebhook.loading.value" :disabled="!form.webhook_url">保存</el-button>
              <el-button :loading="testPush.loading.value" :disabled="!data.push.configured" @click="onTestPush">发送测试消息</el-button>
            </div>
          </el-form>
        </el-card>

        <!-- 大模型 -->
        <el-card shadow="never">
          <template #header>
            <div class="card-title">
              <span>大模型 · OpenRouter</span>
              <el-tag :type="data.llm.key.configured ? 'success' : 'info'" round>
                {{ data.llm.key.configured ? `key 已配置 ${data.llm.key.masked}` : 'key 未配置' }}
              </el-tag>
            </div>
          </template>
          <p class="muted" style="margin-top: 0">
            当前后端：<b>{{ data.llm.provider }}</b>
            <template v-if="data.llm.key.configured"> · key 来源：{{ data.llm.key.source_label }}</template>
          </p>
          <el-alert v-for="n in data.llm.notes" :key="n" :title="n" type="warning" show-icon :closable="false" style="margin-bottom: 10px" />

          <el-form label-position="top" @submit.prevent="onSaveKey">
            <el-form-item label="OpenRouter API key（只写不读：保存后页面只显示末 4 位）">
              <el-input v-model="form.api_key" type="password" placeholder="sk-or-v1-..." autocomplete="new-password" />
            </el-form-item>
            <el-alert v-if="saveKey.error.value" :title="saveKey.error.value" type="error" show-icon :closable="false" />
            <div class="actions">
              <el-button type="primary" native-type="submit" :loading="saveKey.loading.value" :disabled="!form.api_key">保存 key</el-button>
            </div>
          </el-form>

          <el-divider />

          <el-form label-position="top" @submit.prevent="onSaveModels">
            <el-form-item label="主模型（盘后报告）">
              <el-input v-model="form.model" :placeholder="data.llm.model.default" autocomplete="off" clearable />
              <div class="muted">当前：{{ data.llm.model.value ? `${data.llm.model.value}（${data.llm.model.source_label}）` : '使用默认值' }}。留空 = 使用默认。</div>
            </el-form-item>
            <el-form-item label="哨兵模型（盘中情景，每天最多约 15 次，可选更便宜的）">
              <el-input v-model="form.sentinel_model" :placeholder="data.llm.sentinel_model.default" autocomplete="off" clearable />
              <div class="muted">当前：{{ data.llm.sentinel_model.value ? `${data.llm.sentinel_model.value}（${data.llm.sentinel_model.source_label}）` : '使用默认值' }}。留空 = 使用默认。</div>
            </el-form-item>
            <el-alert v-if="saveModels.error.value" :title="saveModels.error.value" type="error" show-icon :closable="false" />
            <div class="actions">
              <el-button type="primary" native-type="submit" :loading="saveModels.loading.value">保存模型</el-button>
            </div>
          </el-form>

          <el-divider />

          <div class="actions" style="margin-top: 0">
            <el-button :loading="checkLlm.loading.value" @click="onCheck">测试连接</el-button>
            <el-button type="danger" plain :loading="clearKey.loading.value" :disabled="data.llm.key.source !== 'page'" @click="onClearKey">清除页面保存的 key</el-button>
            <span class="muted">测试会发一次真实请求，约几分钱，最长等 90 秒。</span>
          </div>
          <el-alert v-if="checkLlm.error.value" :title="checkLlm.error.value" type="error" show-icon :closable="false" style="margin-top: 12px" />
          <el-alert v-if="clearKey.error.value" :title="clearKey.error.value" type="error" show-icon :closable="false" style="margin-top: 12px" />
          <el-alert v-if="checkResult.shown" :type="checkResult.ok ? 'success' : 'error'" :title="checkResult.ok ? '连接正常' : '连接失败'" show-icon :closable="false" style="margin-top: 12px">
            <pre class="check-lines">{{ checkResult.lines.join('\n') }}</pre>
          </el-alert>
          <p class="muted">key 保存在服务器 server/data/private/（权限 0600，不进 git）。网页是自签名 HTTPS 时浏览器会有证书警告，属正常；不要在不信任的网络下使用。</p>
        </el-card>

        <!-- 备份 -->
        <el-card shadow="never">
          <template #header>
            <div class="card-title">
              <span>私有数据备份</span>
              <el-tag :type="BACKUP_TAG[data.backup.level][0]" round>本机每日快照：{{ BACKUP_TAG[data.backup.level][1] }}</el-tag>
            </div>
          </template>
          <el-alert v-if="data.backup.level === 'warn'" :title="data.backup.text" type="warning" show-icon :closable="false" />
          <p v-else class="muted" style="margin-top: 0">{{ data.backup.text }}</p>
          <el-alert v-if="download.error.value" :title="download.error.value" type="error" show-icon :closable="false" style="margin-top: 10px" />
          <div class="actions">
            <el-button type="primary" :loading="download.loading.value" @click="onDownload">下载备份到我的电脑</el-button>
          </div>
          <p class="muted">持仓与自选、exec-0.2 账本、情景账本、提议与修订、盘后报告都只存在这台服务器上、不进 git。本机快照（每天 17:30，保留 14 份）防的是损坏和误操作，<b>防不了这台机器本身丢失</b>——真正的异地备份是上面这个按钮：隔一段时间点一下，把文件存在你自己的电脑上。归档<b>不含凭据</b>（OpenRouter key、企业微信 webhook），恢复后需要重新填。</p>
        </el-card>

        <!-- 健康 -->
        <el-card shadow="never">
          <template #header>
            <div class="card-title">
              <span>系统健康</span>
              <el-tag :type="healthHead(data.health).tag" round>{{ healthHead(data.health).label }}</el-tag>
            </div>
          </template>
          <p class="muted" style="margin-top: 0">{{ healthHead(data.health).text }}</p>
          <el-alert
            v-if="data.health.last_delivery && !data.health.last_delivery.ok"
            :title="`最近一次告警推送（${fmtAt(data.health.last_delivery.at)}）未送达：${data.health.last_delivery.reason}`"
            type="error" show-icon :closable="false" style="margin-bottom: 10px"
          />
          <p v-else-if="data.health.last_delivery" class="muted">最近一次告警推送（{{ fmtAt(data.health.last_delivery.at) }}）：已送达</p>
          <el-table :data="data.health.checks" size="small" empty-text="暂无检查结果">
            <el-table-column prop="title" label="检查项" min-width="140" />
            <el-table-column label="状态" width="90">
              <template #default="{ row }"><el-tag :type="LEVEL_TAG[row.level] || 'info'" size="small">{{ LEVEL_TEXT[row.level] || row.level }}</el-tag></template>
            </el-table-column>
            <el-table-column label="上次执行" width="130">
              <template #default="{ row }">{{ fmtWhen(row.at) }}</template>
            </el-table-column>
            <el-table-column prop="schedule" label="计划时间" min-width="170" />
            <el-table-column prop="message" label="说明" min-width="260" />
          </el-table>
          <p class="muted">每 5 分钟检查一遍：看的是“该出现的产出有没有出现”（盘中引擎最后一轮、日线报告、盘后 AI 研判、备份、证书、磁盘……），不只是进程有没有退出。问题会推企业微信（warn 要连续两次才推，crit 立即推）。<b>这台机器整个挂了它发不出告警</b>——仓库里的 GitHub Actions 会定期访问 /health/deep，异常时 GitHub 给你发邮件。</p>
        </el-card>
      </template>
    </div>
  </div>
</template>

<style scoped>
.check-lines { white-space: pre-wrap; word-break: break-word; margin: 6px 0 0; font-family: var(--as-mono); font-size: 12px; }
</style>
