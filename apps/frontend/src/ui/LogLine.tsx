// src/components/LogLine.tsx 수정
export const LogLine = ({ text }: { text: string }) => {
  return (
    <div className="group flex items-center gap-2 py-0.5 px-2 hover:bg-cyan-500/10 transition-colors">
      <span className="text-cyan-600 font-bold">{">"}</span>
      <span className="text-cyan-500/80 text-[9px] font-mono uppercase tracking-tighter group-hover:text-cyan-400">
        {text}
      </span>
    </div>
  );
};