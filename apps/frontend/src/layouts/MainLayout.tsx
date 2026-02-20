// src/layouts/MainLayout.tsx
import { Outlet } from 'react-router-dom'; // 내부 화면 교체용
import { Sidebar } from '../features/Navigation/Sidebar';
import { TerminalScreen } from '../ui/TerminalScreen';

export function MainLayout() {
  return (
    <div className="flex w-screen h-screen p-4 bg-term-bezel md:p-8">
      {/* 하드웨어 외장 케이스 역할 */}
      <div className="flex w-full h-full max-w-6xl mx-auto border-4 border-black rounded-xl shadow-2xl bg-[#111]">
        
        {/* 접이식 사이드바 컴포넌트 */}
        <Sidebar />

        {/* 메인 화면 영역 */}
        <main className="flex-1 p-4">
          <TerminalScreen>
            {/* 이 위치에 /scanner, /map 등의 URL에 맞는 컴포넌트가 렌더링됨 */}
            <Outlet /> 
          </TerminalScreen>
        </main>
      </div>
    </div>
  );
}