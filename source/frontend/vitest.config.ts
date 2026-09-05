/**
 * vitest 独立配置：
 * 刻意不复用 vite.config.ts —— 那里的 buildAlignPlugin 会在 closeBundle 时
 * 把 dist/* 同步到 backend/static/（构建期防呆），单测阶段不应触发该副作用。
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
