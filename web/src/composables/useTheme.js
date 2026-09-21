import { ref } from 'vue'

// 主题：auto（跟随系统）/ light / dark。Element Plus 的暗色靠 <html class="dark">。
const KEY = 'as-theme'
export const themeMode = ref('auto')

function systemDark() {
  return typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
}

function apply() {
  const dark = themeMode.value === 'dark' || (themeMode.value === 'auto' && systemDark())
  document.documentElement.classList.toggle('dark', dark)
}

export function initTheme() {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === 'light' || saved === 'dark' || saved === 'auto') themeMode.value = saved
  } catch (e) { /* 隐私模式等：用默认值 */ }
  apply()
  if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', apply)
}

export function setTheme(mode) {
  themeMode.value = mode
  try { localStorage.setItem(KEY, mode) } catch (e) { /* ignore */ }
  apply()
}
