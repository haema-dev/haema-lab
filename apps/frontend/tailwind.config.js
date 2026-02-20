/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      // 1. [나의 설계] 시맨틱 색상 통제소
      colors: {
        term: {
          bg: '#080a0c',        // 단말기 기본 배경
          bezel: '#1a1a24',     // 하드웨어 베젤
          cyan: '#4db6ac',      // 정상/정보 텍스트
          amber: '#ffb347',     // 경고 텍스트
          red: '#e53935',       // 치명적 오류
        }
      },
      // 2. [당신의 설계] 기계적 폰트 강제
      fontFamily: {
        mono: ['Courier New', 'monospace'],
      },
      // 3. [통합 설계] 애니메이션 병합
      animation: {
        'pulse-fast': 'pulse 0.6s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'pulse-slow': 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'scan-vertical': 'scan-vertical 1.8s cubic-bezier(0.4, 0, 0.2, 1) infinite',
        'blink': 'blink 1.2s step-end infinite',
        'crt-flicker': 'flicker 0.15s infinite', // 단말기 깜빡임 추가
      },
      // 4. [통합 설계] 키프레임 병합
      keyframes: {
        'scan-vertical': {
          '0%': { top: '0', opacity: '0.9' },
          '100%': { top: '100%', opacity: '0' },
        },
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.4' },
        },
        flicker: {
          '0%, 100%': { opacity: '0.95' },
          '50%': { opacity: '0.88' },
        }
      },
      // 5. [당신의 설계] 물리적 발광 효과 보존
      boxShadow: {
        'neon-green': '0 0 10px rgba(5, 150, 105, 0.5), 0 0 20px rgba(5, 150, 105, 0.3)',
        'neon-red': '0 0 10px rgba(239, 68, 68, 0.5), 0 0 20px rgba(239, 68, 68, 0.3)',
        'neon-amber': '0 0 10px rgba(245, 158, 11, 0.5), 0 0 20px rgba(245, 158, 11, 0.3)',
      },
    },
  },
  plugins: [],
}