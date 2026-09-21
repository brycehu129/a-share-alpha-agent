import { ref, onMounted, onBeforeUnmount } from 'vue'

// 页面数据的加载：data / loading / error + reload()。可选 intervalMs 用于轮询（组件卸载时自动停）。
export function useLoad(fetcher, { intervalMs = 0, immediate = true } = {}) {
  const data = ref(null)
  const loading = ref(false)
  const error = ref('')
  let timer = null

  async function reload({ silent = false } = {}) {
    if (!silent) loading.value = true
    try {
      data.value = await fetcher()
      error.value = ''
    } catch (e) {
      error.value = e.message || String(e)
    } finally {
      loading.value = false
    }
  }
  function stop() {
    if (timer) clearInterval(timer)
    timer = null
  }
  function start() {
    stop()
    // 页面在后台标签页时不轮询：没人看，白打后端和外部行情接口。
    if (intervalMs > 0) timer = setInterval(() => { if (!document.hidden) reload({ silent: true }) }, intervalMs)
  }
  onMounted(() => {
    if (immediate) reload()
    start()
  })
  onBeforeUnmount(stop)
  return { data, loading, error, reload, start, stop }
}
