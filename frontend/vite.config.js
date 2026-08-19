import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"

export default defineConfig({
  base: "./",
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: { echarts: ["echarts/core", "echarts/charts", "echarts/components", "echarts/renderers"] },
      },
    },
  },
})
