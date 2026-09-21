<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { Sunny, Moon, Monitor } from '@element-plus/icons-vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { NAV } from './router'
import { themeMode, setTheme } from './composables/useTheme'

const route = useRoute()
const active = computed(() => route.path)

const ORDER = ['auto', 'light', 'dark']
const LABEL = { auto: '跟随系统', light: '浅色', dark: '深色' }
const themeIcon = computed(() => ({ auto: Monitor, light: Sunny, dark: Moon })[themeMode.value])
function cycleTheme() {
  setTheme(ORDER[(ORDER.indexOf(themeMode.value) + 1) % ORDER.length])
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <header class="topbar">
      <div class="topbar-inner">
        <router-link to="/dashboard" class="brand">Alpha Shadow</router-link>
        <!-- 唯一的一份导航：所有页面都在这里，位置与条目永远一致 -->
        <el-menu mode="horizontal" :default-active="active" router :ellipsis="false" class="nav" aria-label="主导航">
          <el-menu-item v-for="n in NAV" :key="n.path" :index="n.path">{{ n.title }}</el-menu-item>
        </el-menu>
        <el-tooltip :content="`主题：${LABEL[themeMode]}（点击切换）`" placement="bottom">
          <el-button circle :icon="themeIcon" :aria-label="`切换主题，当前：${LABEL[themeMode]}`" @click="cycleTheme" />
        </el-tooltip>
      </div>
    </header>
    <main class="container">
      <router-view />
    </main>
  </el-config-provider>
</template>

<style>
.topbar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-light);
}
.topbar-inner {
  max-width: 1180px;
  margin: 0 auto;
  padding: 0 20px;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 16px;
}
.brand {
  font-weight: 700;
  font-size: 17px;
  letter-spacing: -0.01em;
  color: var(--el-text-color-primary);
  text-decoration: none;
  white-space: nowrap;
}
/* 窄屏不折叠成「···」：六个入口始终全部可见，放不下时横向滑动 */
.nav.el-menu { flex: 1; min-width: 0; border-bottom: none; background: transparent; height: 56px; overflow-x: auto; overflow-y: hidden; scrollbar-width: none; }
.nav.el-menu::-webkit-scrollbar { display: none; }
.nav .el-menu-item { font-weight: 600; height: 56px; flex: none; }
@media (max-width: 640px) {
  .topbar-inner { padding: 0 12px; gap: 8px; }
  .brand { display: none; }
}
</style>
