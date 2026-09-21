import { ref } from 'vue'

// 包装一次「点按钮 → 调接口」：自动管理 loading，成功弹 ElMessage，失败把错误留在 error 里
// （页面用 el-alert 常驻显示，比一闪而过的 toast 更不容易漏看）。
export function useAction() {
  const loading = ref(false)
  const error = ref('')
  async function run(fn, successMessage) {
    loading.value = true
    error.value = ''
    try {
      const result = await fn()
      if (successMessage !== false) {
        ElMessage.success(typeof successMessage === 'function' ? successMessage(result) : successMessage || (result && result.message) || '完成')
      }
      return result
    } catch (e) {
      error.value = e.message || String(e)
      return null
    } finally {
      loading.value = false
    }
  }
  return { loading, error, run }
}
