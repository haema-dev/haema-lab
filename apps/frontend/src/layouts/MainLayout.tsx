// src/layouts/MainLayout.tsx
import { useState } from 'react';
import type { ReactNode } from 'react';
import { Sidebar } from '../features/Navigation/Sidebar';
import TerminalFrame from '../ui/TerminalFrame';

interface MainLayoutProps {
  children: ReactNode;
}

export function MainLayout({ children }: MainLayoutProps) {
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);

  return (
    <div className="app-container">
      {/* 사이드바 */}
      <Sidebar isOpen={isSidebarOpen} onToggle={() => setIsSidebarOpen(!isSidebarOpen)} />

      {/* 모바일 전용: 사이드바 닫혀있을 때 열기 버튼 */}
      {!isSidebarOpen && (
        <button
          className="mobile-sidebar-open-btn"
          onClick={() => setIsSidebarOpen(true)}
          title="사이드바 열기"
        >
          ▶
        </button>
      )}

      {/* 모바일 전용: 사이드바 열려있을 때 배경 오버레이 (터치하면 닫힘) */}
      {isSidebarOpen && (
        <div
          className="sidebar-overlay"
          onClick={() => setIsSidebarOpen(false)}
        />
      )}

      {/* 메인 단말기 영역 */}
      <main className="main-terminal-wrapper">
        <TerminalFrame>
          {children}
        </TerminalFrame>

        {/* 하단 장식 베젤 */}
        <div className="mobile-nav-bezel">
          <div className="indicator-light red"></div>
          <div className="indicator-light green blinking"></div>
        </div>
      </main>
    </div>
  );
}