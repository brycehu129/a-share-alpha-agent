import { createRouter, createWebHistory } from 'vue-router'

// 所有页面共用 App.vue 里的同一个顶栏；菜单项由这张表生成，所以任何页面上导航都一模一样。
export const NAV = [
  { path: '/dashboard', title: '看板', component: () => import('./views/DashboardView.vue') },
  { path: '/candidates', title: '候选池', component: () => import('./views/CandidatesView.vue') },
  { path: '/book', title: '持仓与自选', component: () => import('./views/BookView.vue') },
  { path: '/proposals', title: '提议', component: () => import('./views/ProposalsView.vue') },
  { path: '/settings', title: '设置', component: () => import('./views/SettingsView.vue') },
]

// 已并入其它页面的旧地址：哨兵的告警详情挂在「持仓与自选」每一行，盘后分析是看板的一个 tab。
// 旧书签、以及企业微信推送里写的「完整报告见后台 /postclose」都还要能打开（服务端 SPA_ROUTES 里也保留了它们）。
const LEGACY = [
  { path: '/sentinel', redirect: '/book' },
  { path: '/postclose', redirect: { path: '/dashboard', query: { tab: 'postclose' } } },
]

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    ...NAV.map((n) => ({ path: n.path, component: n.component, meta: { title: n.title } })),
    ...LEGACY,
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  document.title = `${to.meta.title || 'Alpha Shadow'} · Alpha Shadow`
})

export default router
