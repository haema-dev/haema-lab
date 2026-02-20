import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';

export default function BootSequence({ onComplete }: { onComplete: () => void }) {
  const [logs, setLogs] = useState<string[]>([]);
  const bootMessages = [
    "> KERNEL_INIT... [OK]",
    "> CONNECTING_TO_EDGE_DB... SUCCESS",
    "> SYNCING_BIO_METRICS... PENDING",
    "> LOADING_SCANNER_MODULES...",
    "> AUTH_KEY_ACCEPTED"
  ];

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
    <div className="w-80 bg-black border-2 border-cyan-900/50 p-6 font-mono text-[10px] text-cyan-500 shadow-[0_0_20px_rgba(6,182,212,0.2)]">
      <div className="flex flex-col gap-1">
        {logs.map((log, idx) => (
          <motion.p initial={{ opacity: 0, x: -5 }} animate={{ opacity: 1, x: 0 }} key={idx}>
            {log}
          </motion.p>
        ))}
        <motion.div animate={{ opacity: [0, 1] }} transition={{ repeat: Infinity, duration: 0.5 }} className="w-2 h-3 bg-cyan-500 mt-1" />
      </div>
    </div>
  );
}