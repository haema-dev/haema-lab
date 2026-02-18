import { useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';

// 통합된 컴포넌트 임포트
import TerminalFrame from './components/TerminalFrame';
import TerminalLogin from './components/TerminalLogin';
import BootSequence from './components/BootSequence';
import TerminalScanner from './components/TerminalScanner';

// CSS 스타일 (이미 정의된 경우 생략 가능)
import './App.css';

/**
 * @description 단말기 시스템의 메인 컨트롤러
 * @tech_stack React 19, Vite, TypeScript, Framer Motion
 */
export default function App() {
  // 시스템 페이즈 상태 관리
  const [phase, setPhase] = useState<'auth' | 'boot' | 'main'>('auth');

  return (
    <div className="min-h-screen bg-black flex items-center justify-center p-4 overflow-hidden font-mono">
      {/* 1. 물리적 단말기 프레임: 모든 UI의 'chassis' 역할 */}
      <TerminalFrame>
        {/* 2. 페이즈 전환 애니메이션: 'mode="wait"'로 이전 화면이 완전히 사라진 후 다음 화면 등장 */}
        <AnimatePresence mode="wait">
          
          {/* PHASE: AUTH - 시스템 권한 승인 단계 */}
          {phase === 'auth' && (
            <motion.div
              key="auth-phase"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0, x: -20, filter: "blur(10px)" }}
              transition={{ duration: 0.4 }}
              className="h-full w-full"
            >
              <TerminalLogin onLoginSuccess={() => setPhase('boot')} />
            </motion.div>
          )}

          {/* PHASE: BOOT - 화면 간의 시각적 연결 고리 (부팅 시퀀스) */}
          {phase === 'boot' && (
            <motion.div
              key="boot-phase"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0, filter: "brightness(3)" }}
              transition={{ duration: 0.3 }}
              className="h-full w-full flex items-center justify-center bg-black"
            >
              <BootSequence onComplete={() => setPhase('main')} />
            </motion.div>
          )}

          {/* PHASE: MAIN - 바이오/경제 데이터 스캐너 대시보드 */}
          {phase === 'main' && (
            <motion.div
              key="main-phase"
              initial={{ opacity: 0, scale: 1.1, filter: "brightness(0)" }}
              animate={{ opacity: 1, scale: 1, filter: "brightness(1)" }}
              transition={{ duration: 0.8, ease: "easeOut" }}
              className="h-full w-full"
            >
              <TerminalScanner />
            </motion.div>
          )}

        </AnimatePresence>
      </TerminalFrame>
    </div>
  );
}