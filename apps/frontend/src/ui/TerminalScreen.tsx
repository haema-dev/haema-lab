// src/ui/TerminalScreen.tsx
import React from 'react';

interface TerminalScreenProps {
  children: React.ReactNode;
  hasScanlines?: boolean; // 스캔라인 토글
  dangerMode?: boolean;   // 붉은 화면 토글
}

export function TerminalScreen({ children, hasScanlines = true, dangerMode = false }: TerminalScreenProps) {
  return (
    <div className={`relative w-full h-full overflow-hidden rounded-md border-2 
      ${dangerMode ? 'border-term-red bg-red-950/20' : 'border-term-bezel bg-term-bg'}`}>
      
      {/* 1. 스캔라인 레이어 (선택적 렌더링) */}
      {hasScanlines && <div className="absolute inset-0 pointer-events-none opacity-20 scanlines-effect" />}
      
      {/* 2. 실제 콘텐츠 영역 (플리커 효과 포함) */}
      <div className="relative z-10 w-full h-full p-4 text-term-cyan animate-crt-flicker overflow-y-auto custom-scrollbar">
        {children}
      </div>
    </div>
  );
}