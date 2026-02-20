// src/features/Navigation/Sidebar.tsx
import { NavLink } from 'react-router-dom';

interface SidebarProps {
  isOpen: boolean;
  onToggle: () => void;
}

export function Sidebar({ isOpen, onToggle }: SidebarProps) {
  return (
    <aside
      className={`sidebar-aside ${isOpen ? 'open' : 'closed'}`}
    >
      {/* 물리 토글 버튼 — 사이드바 내부에 배치 */}
      <button
        onClick={onToggle}
        className="sidebar-hw-toggle"
        title={isOpen ? '사이드바 닫기' : '사이드바 열기'}
      >
        {isOpen ? '◀' : '▶'}
      </button>

      {/* 내부 콘텐츠 (닫혔을 때 숨김 처리) */}
      <div className={`sidebar-inner ${isOpen ? '' : 'collapsed'}`}>

        {/* 상단 네비게이션 */}
        <div className="sidebar-nav-section">
          <h3 className="sidebar-title">
            SYS. NAVIGATOR
          </h3>
          <nav className="sidebar-nav-list">
            <NavLink
              to="/scanner"
              className={({ isActive }) => `sys-nav-link ${isActive ? 'active' : ''}`}
            >
              [01] BIO_SCANNER
            </NavLink>
            <NavLink
              to="/map"
              className={({ isActive }) => `sys-nav-link ${isActive ? 'active' : ''}`}
            >
              [02] SECTOR_MAP
            </NavLink>
            <NavLink
              to="/logs"
              className={({ isActive }) => `sys-nav-link ${isActive ? 'active' : ''}`}
            >
              [03] DATA_LOGS
            </NavLink>
          </nav>
        </div>

        {/* 하단 상태 정보 */}
        <div className="sidebar-status">
          <p>UNIT ID: Z-SURV-774</p>
          <p className="status-secure">LINK: SECURE</p>
          <p className="status-battery">BAT: 84% [|||||| ]</p>
        </div>
      </div>
    </aside>
  );
}