// TerminalFrame.tsx
import React, { ReactNode } from 'react';
import '../styles/TerminalFrame.css';

interface TerminalFrameProps {
  children: ReactNode;
}

const TerminalFrame: React.FC<TerminalFrameProps> = ({ children }) => {
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