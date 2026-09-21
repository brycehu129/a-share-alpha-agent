import { computed } from 'vue'
import { get } from '../api'
import { useLoad } from './useLoad'

// 看板和候选池共用同一份 /api/dashboard。服务器每次请求时从 GitHub market-data 分支拉
// dashboard/latest.json（30 秒缓存）；「刷新」带 refresh=1 绕过缓存强制重拉。
// 两个页面各自请求一次，服务端缓存保证不会重复打 GitHub。
export function useDashboard() {
  let force = false
  const { data: resp, loading, error, reload } = useLoad(() => {
    const q = force ? '?refresh=1' : ''
    force = false
    return get('/api/dashboard' + q)
  })
  function refresh() {
    force = true
    reload()
  }
  const d = computed(() => (resp.value ? resp.value.data : null))
  // agent 可能整体缺失（导出还没生成过、或只拿到了行情）：所有依赖它的地方都必须能处理 null。
  const agent = computed(() => (d.value ? d.value.agent || null : null))
  return { resp, d, agent, loading, error, reload, refresh }
}
