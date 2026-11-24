import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
    open: true,
    // 로컬 개발: /api 를 백엔드로 넘긴다. 운영에서는 Gateway 가 /api/** 를 Django 로 보내고 X-User-Id 를 넣는다.
    // 대상은 VITE_API_PROXY_TARGET (기본: 쿠버 백엔드 NodePort). Gateway 처럼 테스트 유저 1 을 넣는다.
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET ?? 'http://192.168.45.24:30081',
        changeOrigin: true,
        headers: { 'X-User-Id': '1' },
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true
  }
})