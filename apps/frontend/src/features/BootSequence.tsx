// src/features/BootSequence.tsx
import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';

const bootMessages = [
  "> KERNEL_INIT... [OK]",
  "> CONNECTING_TO_EDGE_DB... SUCCESS",
  "> SYNCING_BIO_METRICS... PENDING",
  "> LOADING_SCANNER_MODULES...",
  "> AUTH_KEY_ACCEPTED"
];

export default function BootSequence({ onComplete }: { onComplete: () => void }) {
  const [logs, setLogs] = useState<string[]>([]);

  useEffect(() => {
    let i = 0;
    const interval = setInterval(() => {
      if (i < bootMessages.length) {
        setLogs(prev => [...prev, bootMessages[i]]);
        i++;
      } else {
        clearInterval(interval);
        setTimeout(onComplete, 800);
      }
    }, 150);
    return () => clearInterval(interval);
  }, [onComplete]);

  return (
    <div className="boot-sequence-container">
      <div className="boot-log-list">
        {logs.map((log, idx) => (
          <motion.p initial={{ opacity: 0, x: -5 }} animate={{ opacity: 1, x: 0 }} key={idx} className="boot-log-line">
            {log}
          </motion.p>
        ))}
        <motion.div animate={{ opacity: [0, 1] }} transition={{ repeat: Infinity, duration: 0.5 }} className="boot-cursor" />
      </div>
    </div>
  );
}