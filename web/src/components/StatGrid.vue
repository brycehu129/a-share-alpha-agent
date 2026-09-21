<template>
  <!-- 用 1px 间隙 + 边框底色画出格线；子元素放 StatTile -->
  <div class="stat-grid"><slot /></div>
</template>

<style scoped>
.stat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  background: var(--el-border-color-light);
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  overflow: hidden;
}
</style>
