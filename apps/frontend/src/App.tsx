// src/App.tsx
import { useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import './styles/App.css';
import TerminalFrame from './ui/TerminalFrame';
import BootSequence from './features/BootSequence';
import TerminalLogin from './features/TerminalLogin';
import TerminalScanner from './pages/TerminalScanner';
import { MainLayout } from './layouts/MainLayout';

export default function App() {
  const [booted, setBooted] = useState(false);
  const [loggedIn, setLoggedIn] = useState(false);

  // 인증 전 화면 (부트 시퀀스 → 로그인)
  if (!loggedIn) {
    return (
      <div className="app-container">
        <div className="login-frame-wrapper">
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

  // 인증 완료 후 메인 레이아웃 + 라우터
  return (
    <BrowserRouter>
      <MainLayout>
        <Routes>
          <Route path="/" element={<Navigate to="/scanner" replace />} />
          <Route path="/scanner" element={<TerminalScanner />} />
          <Route path="/map" element={<div className="placeholder-screen cyan">GPS_MODULE_OFFLINE... AWAITING SATELLITE LINK</div>} />
          <Route path="/logs" element={<div className="placeholder-screen amber">ACCESSING_LOCAL_DATABASE...</div>} />
          <Route path="*" element={<div className="placeholder-screen red">ERR: MODULE_NOT_FOUND</div>} />
        </Routes>
      </MainLayout>
    </BrowserRouter>
  );
}