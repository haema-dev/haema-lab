// src/ui/TerminalFrame.tsx
import type { ReactNode } from 'react';
import '../styles/TerminalFrame.css';

interface TerminalFrameProps {
  children: ReactNode;
}

const TerminalFrame = ({ children }: TerminalFrameProps) => {
  return (
    <div className="terminal-bezel">
      <div className="terminal-screen-container">
        <div className="terminal-screen scanlines">
          <div className="terminal-content flicker">
            {children}
          </div>
        </div>
      </div>
    </div>
  );
};

export default TerminalFrame;