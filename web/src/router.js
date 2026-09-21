import { createRouter, createWebHistory } from 'vue-router'

// 六个页面共用 App.vue 里的同一个顶栏；菜单项由这张表生成，所以任何页面上导航都一模一样。
export const NAV = [
  { path: '/dashboard', title: '看板', component: () => import('./views/DashboardView.vue') },
  { path: '/sentinel', title: '哨兵', component: () => import('./views/SentinelView.vue') },
  { path: '/proposals', title: '提议', component: () => import('./views/ProposalsView.vue') },
  { path: '/postclose', title: '盘后分析', component: () => import('./views/PostcloseView.vue') },
  { path: '/book', title: '持仓与自选', component: () => import('./views/BookView.vue') },
  { path: '/settings', title: '设置', component: () => import('./views/SettingsView.vue') },
]

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    ...NAV.map((n) => ({ path: n.path, component: n.component, meta: { title: n.title } })),
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  document.title = `${to.meta.title || 'Alpha Shadow'} · Alpha Shadow`
})

export default router
