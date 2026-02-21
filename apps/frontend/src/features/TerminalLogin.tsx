// src/features/TerminalLogin.tsx
import { useState } from 'react';
import { motion } from 'framer-motion';
import { LogLine } from '../ui/LogLine';
import '../styles/TerminalLogin.css';

export default function TerminalLogin({ onLoginSuccess }: { onLoginSuccess: () => void }) {
  const [accessCode, setAccessCode] = useState('2026');
  const [isVerifying, setIsVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const systemLogs = [
    "INITIALIZING_HANDSHAKE...",
    "CONNECTING_TO_EDGE_DB... SUCCESS",
    "SYNCING_BIO_METRICS... PENDING",
    "NODE_ENCRYPTION_ACTIVE",
    "BYPASSING_RESTRICTIONS..."
  ];

  return (
    <div className="login-container">
      {/* 카본 텍스처 배경 */}
      <div className="login-bg-texture" />

      <motion.div
        initial={{ opacity: 0, scale: 0.9 }}
        animate={{ opacity: 1, scale: 1 }}
        className="login-content-wrapper"
      >
        <div className="login-header">
          <h2>SYSTEM_AUTH</h2>
          <div className="header-divider" />
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!accessCode) return;
            setIsVerifying(true);
            setError(null);

            setTimeout(() => {
              if (accessCode === '2026') {
                onLoginSuccess();
              } else {
                setError("ACCESS_DENIED: UNAUTHORIZED_SIGNAL");
                setIsVerifying(false);
              }
            }, 2000);
          }}
          className="login-form"
        >
          <div className="input-group">
            <label>Protocol Key</label>
            <input
              type="password"
              value={accessCode}
              onChange={(e) => setAccessCode(e.target.value)}
              placeholder="____"
              className="protocol-input"
            />
          </div>

          {error && <div className="error-message">{error}</div>}

          <button
            type="submit"
            disabled={isVerifying}
            className={`login-submit-btn ${isVerifying ? 'verifying' : ''}`}
          >
            <span className="btn-label">
              {isVerifying ? "Verifying..." : "Authorize Access"}
            </span>
          </button>
        </form>

        <div className="system-feed-container">
          <p className="feed-title">SYSTEM_FEED //</p>
          <div className="feed-logs">
            {systemLogs.map((log, i) => <LogLine key={i} text={log} />)}
          </div>
        </div>
      </motion.div>
    </div>
  );
}