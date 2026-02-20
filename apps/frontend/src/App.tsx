// src/App.tsx
import React, { useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate, NavLink } from 'react-router-dom';
import './styles/App.css'; // 경로 확인 필수
import TerminalFrame from './ui/TerminalFrame';
import BootSequence from './features/BootSequence';
import TerminalLogin from './features/TerminalLogin';
import TerminalScanner from './pages/TerminalScanner';

// --- [라우팅 레이아웃 컴포넌트] ---
// 로그인 성공 이후 보여질 실제 단말기 UI (사이드바 + 메인 화면)
function MainLayout({ isSidebarOpen, toggleSidebar }: { isSidebarOpen: boolean, toggleSidebar: () => void }) {
  return (
    <div className="app-container">
      {/* 1. 동적 사이드바 (Navigation) */}
      <aside className={`hardware-sidebar ${isSidebarOpen ? 'open' : 'closed'}`}>
        <button className="sidebar-toggle-btn" onClick={toggleSidebar} title="Toggle Nav">
          {isSidebarOpen ? '◀' : '▶'}
        </button>

        <div className="sidebar-content">
          <div className="sidebar-panel">
            <h3>SYS. NAVIGATOR</h3>
            {/* App.tsx 내부의 nav 영역 수정 */}
            <nav className="flex flex-col gap-3 mt-4">
              <p>
                <NavLink 
                  to="/scanner" 
                  className={({ isActive }) => `sys-nav-link ${isActive ? 'active' : ''}`}
                >
                  [01] BIO_SCANNER
                </NavLink>
              </p>
              <p>
                <NavLink 
                  to="/map" 
                  className={({ isActive }) => `sys-nav-link ${isActive ? 'active' : ''}`}
                >
                  [02] SECTOR_MAP
                </NavLink>
              </p>
              <p>
                <NavLink 
                  to="/logs" 
                  className={({ isActive }) => `sys-nav-link ${isActive ? 'active' : ''}`}
                >
                  [03] DATA_LOGS
                </NavLink>
              </p>
            </nav>
          </div>
          
          <div className="sidebar-panel info-panel mt-auto">
            <small>UNIT ID: Z-SURV-774</small>
            <small className="text-emerald-500 block mt-1">LINK: SECURE</small>
          </div>
        </div>

        {isSidebarOpen && (
          <>
            <div className="screw-head top-left"></div>
            <div className="screw-head bottom-right"></div>
          </>
        )}
      </aside>

      {/* 2. 메인 단말기 영역 (콘텐츠 스위칭) */}
      <main className="main-terminal-wrapper">
        <TerminalFrame>
          <Routes>
            <Route path="/" element={<Navigate to="/scanner" replace />} />
            <Route path="/scanner" element={<TerminalScanner />} />
            <Route path="/map" element={<div className="p-6 text-cyan-500 animate-pulse">GPS_MODULE_OFFLINE... AWAITING SATELLITE LINK</div>} />
            <Route path="/logs" element={<div className="p-6 text-amber-500">ACCESSING_LOCAL_DATABASE...</div>} />
            {/* 404 처리 */}
            <Route path="*" element={<div className="p-6 text-red-500">ERR: MODULE_NOT_FOUND</div>} />
          </Routes>
        </TerminalFrame>
        
        {/* 모바일 장식 베젤 */}
        <div className="mobile-nav-bezel">
             <div className="indicator-light red"></div>
             <div className="indicator-light green blinking"></div>
        </div>
      </main>
    </div>
  );
}

// --- [최상위 App 컴포넌트] ---
export default function App() {
  const [booted, setBooted] = useState(false);
  const [loggedIn, setLoggedIn] = useState(false);
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);

  // 인증 전 화면 (부팅 & 로그인)
  if (!loggedIn) {
    return (
      <div className="app-container" style={{ justifyContent: 'center' }}>
        <div className="main-terminal-wrapper" style={{ flex: 'none', width: '100%', maxWidth: '800px', margin: '0 auto' }}>
          <TerminalFrame>
            {!booted ? (
              <BootSequence onComplete={() => setBooted(true)} />
            ) : (
              <TerminalLogin onLoginSuccess={() => setLoggedIn(true)} />
            )}
          </TerminalFrame>
        </div>
      </div>
    );
  }

  // 인증 완료 후 라우터 기반 메인 레이아웃 렌더링
  return (
    <BrowserRouter>
      <MainLayout 
        isSidebarOpen={isSidebarOpen} 
        toggleSidebar={() => setIsSidebarOpen(!isSidebarOpen)} 
      />
    </BrowserRouter>
  );
}