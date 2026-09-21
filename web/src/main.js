import { createApp } from 'vue'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/tokens.css'
import App from './App.vue'
import router from './router'
import { initTheme } from './composables/useTheme'

initTheme()
createApp(App).use(router).mount('#app')
