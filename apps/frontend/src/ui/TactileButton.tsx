// src/components/TactileButton.tsx
import { motion } from 'framer-motion';

interface TactileButtonProps {
  label: string;
  onClick?: () => void; // 선택 사항으로 변경
  disabled?: boolean;
  variant?: 'cyan' | 'red' | 'zinc';
  type?: 'button' | 'submit'; // type 추가
}

export function TactileButton({ label, onClick, disabled, variant = 'cyan', type = 'button' }: TactileButtonProps) {
  const themes = {
    cyan: 'border-cyan-950 text-cyan-400 group-hover:text-cyan-300',
    red: 'border-red-950 text-red-500 group-hover:text-red-400',
    zinc: 'border-zinc-950 text-zinc-500 group-hover:text-zinc-300'
  };

  return (
    <motion.button 
      type={type} // 이 부분이 있어야 로그인이 넘어갑니다.
      whileTap={{ scale: 0.98, y: 2 }}
      onClick={onClick}
      disabled={disabled}
      className={`relative w-full px-6 py-4 bg-zinc-900 border-b-4 ${themes[variant]} active:border-b-0 transition-all rounded-xl group overflow-hidden disabled:opacity-50`}
    >
      <div className="absolute inset-0 bg-white/5 opacity-0 group-hover:opacity-100 transition-opacity" />
      <span className="relative z-10 text-[10px] font-black uppercase tracking-[0.3em]">
        {label}
      </span>
    </motion.button>
  );
}