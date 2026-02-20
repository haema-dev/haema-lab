// src/components/TerminalScanner.tsx - 개선된 버전
import { useState } from 'react';
import { useAutoAnimate } from '@formkit/auto-animate/react';
import { Scan, RefreshCw, AlertCircle, Radio, Crosshair } from 'lucide-react';

interface BioEconomicData {
  id: string;
  name: string;
  price: number;
  mutationRate: number;
  proteinSeq: string;
  threat: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  location: string;
}

export default function TerminalScanner() {
  const [parent] = useAutoAnimate<HTMLDivElement>({ duration: 250 });
  const [status, setStatus] = useState<'idle' | 'scanning' | 'result'>('idle');
  const [data, setData] = useState<BioEconomicData | null>(null);

  const startScan = () => {
    setStatus('scanning');
    setTimeout(() => {
      setData({
        id: 'SYNTH_PRO_01',
        name: 'Synthetic Protein Bar',
        price: 50.95,
        mutationRate: 74,
        threat: 'HIGH',
        location: 'SECTOR_4_NORTH',
        proteinSeq: 'MLAV...QQR...PPR...RRA...GGT...CCA...TTA...AAG...CTG...GGA'
      });
      setStatus('result');
    }, 2500);
  };

  return (
    <div className="h-full w-full bg-gradient-to-br from-zinc-950 via-zinc-900 to-black font-mono overflow-hidden">
      
      {/* 상단 시스템 상태바 - 개선된 버전 */}
      <div className="sticky top-0 z-30 px-4 sm:px-6 py-3 bg-zinc-950/80 backdrop-blur-md border-b border-emerald-900/30">
        <div className="flex justify-between items-center text-[9px] sm:text-[10px]">
          <div className="space-y-0.5">
            <p className="text-emerald-500 flex items-center gap-2">
              <Radio size={10} className="animate-pulse" />
              <span>OS: BIO-OS v26.2.15</span>
            </p>
            <p className="text-cyan-400">LINK: SAT_UPLINK_OK</p>
          </div>
          <div className="text-right space-y-0.5">
            <p className="text-zinc-500">BAT: <span className="text-amber-400">[|||||||] 92%</span></p>
            <p className="text-red-400 animate-pulse flex items-center justify-end gap-1">
              <AlertCircle size={10} />
              <span>ALERT: ZONE_4</span>
            </p>
          </div>
        </div>
      </div>

      <div ref={parent} className="h-full px-4 sm:px-6 py-6 overflow-y-auto">
        
        {/* === IDLE / SCANNING VIEW === */}
        {(status === 'idle' || status === 'scanning') && (
          <div className="flex flex-col items-center justify-center min-h-[calc(100vh-200px)] py-8">
            
            {/* 스캔 레티클 (조준선) - 개선된 디자인 */}
            <div className={`relative w-64 h-64 sm:w-72 sm:h-72 transition-all duration-500 ${
              status === 'scanning' ? 'scale-110' : 'scale-100'
            }`}>
              
              {/* 외곽 회전 링 */}
              <div className={`absolute inset-0 border-2 border-emerald-500/20 rounded-full ${
                status === 'scanning' ? 'animate-spin-slow' : ''
              }`}>
                <div className="absolute top-0 left-1/2 w-2 h-2 bg-emerald-500 rounded-full -translate-x-1/2 -translate-y-1/2 shadow-[0_0_10px_rgba(16,185,129,1)]" />
              </div>

              {/* 중앙 타겟 박스 */}
              <div className={`absolute inset-12 border-2 transition-all duration-300 ${
                status === 'scanning' 
                  ? 'border-emerald-400 shadow-[0_0_20px_rgba(16,185,129,0.6),inset_0_0_20px_rgba(16,185,129,0.3)] animate-pulse' 
                  : 'border-emerald-600/30'
              }`}>
                {/* 코너 디테일 */}
                <div className="absolute -top-1 -left-1 w-4 h-4 border-t-4 border-l-4 border-emerald-400" />
                <div className="absolute -top-1 -right-1 w-4 h-4 border-t-4 border-r-4 border-emerald-400" />
                <div className="absolute -bottom-1 -left-1 w-4 h-4 border-b-4 border-l-4 border-emerald-400" />
                <div className="absolute -bottom-1 -right-1 w-4 h-4 border-b-4 border-r-4 border-emerald-400" />
              </div>

              {/* 십자선 */}
              <div className="absolute top-1/2 left-0 w-full h-[2px] bg-gradient-to-r from-transparent via-emerald-500/50 to-transparent" />
              <div className="absolute left-1/2 top-0 h-full w-[2px] bg-gradient-to-b from-transparent via-emerald-500/50 to-transparent" />

              {/* 중앙 아이콘 */}
              <div className="absolute inset-0 flex items-center justify-center">
                <Crosshair 
                  size={48} 
                  className={`${status === 'scanning' ? 'text-emerald-400 animate-pulse' : 'text-emerald-600/50'}`}
                  strokeWidth={1.5}
                />
              </div>

              {/* 스캔 라인 */}
              {status === 'scanning' && (
                <div className="absolute inset-12 overflow-hidden">
                  <div className="absolute w-full h-1 bg-gradient-to-r from-transparent via-emerald-400 to-transparent shadow-[0_0_15px_rgba(16,185,129,1)] animate-scan-down" />
                </div>
              )}
            </div>

            {/* 안내 텍스트 */}
            <div className="mt-10 text-center space-y-3 px-4">
              <p className="text-zinc-400 text-xs sm:text-sm leading-relaxed max-w-xs">
                {status === 'scanning' 
                  ? '// ACQUIRING TARGET DATA...' 
                  : 'ALIGN BIO-SUBJECT WITHIN RETICLE'
                }
              </p>
              {status === 'scanning' && (
                <p className="text-emerald-400 text-[10px] animate-pulse">
                  SCANNING PROTEIN MARKERS...
                </p>
              )}
            </div>

            {/* 스캔 버튼 - 개선된 디자인 */}
            <button 
              onClick={startScan}
              disabled={status === 'scanning'}
              className={`
                mt-10 relative group px-8 py-4 rounded-lg
                bg-gradient-to-br from-emerald-950 to-emerald-900
                border-2 transition-all duration-300
                ${status === 'scanning' 
                  ? 'border-emerald-400 shadow-[0_0_30px_rgba(16,185,129,0.5)] cursor-wait' 
                  : 'border-emerald-600 hover:border-emerald-400 hover:shadow-[0_0_25px_rgba(16,185,129,0.4)] active:scale-95'
                }
              `}
            >
              <span className="relative z-10 flex items-center gap-3 text-sm sm:text-base font-bold uppercase tracking-widest">
                {status === 'scanning' ? (
                  <>
                    <RefreshCw size={18} className="animate-spin text-emerald-400" />
                    <span className="text-emerald-400">SCANNING...</span>
                  </>
                ) : (
                  <>
                    <Scan size={18} className="text-emerald-400" />
                    <span className="text-emerald-300">INITIATE SCAN</span>
                  </>
                )}
              </span>
              {!status && (
                <div className="absolute inset-0 bg-emerald-500/10 opacity-0 group-hover:opacity-100 transition-opacity rounded-lg" />
              )}
            </button>
          </div>
        )}

        {/* === RESULT DASHBOARD === */}
        {status === 'result' && data && (
          <div className="space-y-4 pb-20 animate-fade-in">
            
            {/* 헤더 - 개선된 디자인 */}
            <div className="bg-gradient-to-r from-emerald-950/50 to-transparent border-l-4 border-emerald-500 p-4 rounded-r-lg">
              <p className="text-[9px] text-emerald-600 mb-2 flex items-center gap-2">
                <span className="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-pulse" />
                TARGET_ID: {data.id}
              </p>
              <h1 className="text-xl sm:text-2xl font-black text-emerald-300 uppercase leading-tight tracking-wide">
                {data.name}
              </h1>
              <p className="text-[10px] text-zinc-500 mt-1">LOCATION: {data.location}</p>
            </div>

            {/* 위협 레벨 배너 */}
            <div className={`
              border-2 rounded-lg p-3 flex items-center gap-3
              ${data.threat === 'CRITICAL' ? 'bg-red-950/30 border-red-500/50' :
                data.threat === 'HIGH' ? 'bg-orange-950/30 border-orange-500/50' :
                data.threat === 'MEDIUM' ? 'bg-amber-950/30 border-amber-500/50' :
                'bg-emerald-950/30 border-emerald-500/50'}
            `}>
              <AlertCircle 
                size={24} 
                className={`
                  ${data.threat === 'CRITICAL' ? 'text-red-500' :
                    data.threat === 'HIGH' ? 'text-orange-500' :
                    data.threat === 'MEDIUM' ? 'text-amber-500' :
                    'text-emerald-500'}
                  animate-pulse
                `}
              />
              <div className="flex-1">
                <p className="text-[9px] text-zinc-500 uppercase">Threat Assessment</p>
                <p className={`
                  text-sm font-black uppercase
                  ${data.threat === 'CRITICAL' ? 'text-red-400' :
                    data.threat === 'HIGH' ? 'text-orange-400' :
                    data.threat === 'MEDIUM' ? 'text-amber-400' :
                    'text-emerald-400'}
                `}>
                  {data.threat}
                </p>
              </div>
            </div>

            {/* 메인 스탯 그리드 */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              
              {/* 변이율 카드 */}
              <div className="relative bg-zinc-950/60 border-2 border-red-900/50 rounded-lg p-4 overflow-hidden group hover:border-red-500/70 transition-colors">
                <div className="absolute inset-0 bg-gradient-to-br from-red-950/40 to-transparent opacity-50 group-hover:opacity-80 transition-opacity" />
                <div className="relative z-10">
                  <p className="text-[10px] text-red-400 uppercase mb-2 flex items-center gap-2">
                    <span className="w-2 h-2 bg-red-500 rounded-full animate-pulse" />
                    Mutation Rate (μ)
                  </p>
                  <div className="flex items-end gap-2 mb-3">
                    <span className="text-4xl sm:text-5xl font-black text-red-400 leading-none">{data.mutationRate}</span>
                    <span className="text-red-500 text-xl mb-1">%</span>
                  </div>
                  {/* 프로그레스 바 */}
                  <div className="w-full bg-red-950/50 h-2 rounded-full overflow-hidden">
                    <div 
                      className="h-full bg-gradient-to-r from-red-600 to-red-400 shadow-[0_0_10px_rgba(239,68,68,0.8)] transition-all duration-1000 ease-out"
                      style={{width: `${data.mutationRate}%`}}
                    />
                  </div>
                </div>
              </div>

              {/* 시장가 카드 */}
              <div className="relative bg-zinc-950/60 border-2 border-amber-900/50 rounded-lg p-4 overflow-hidden group hover:border-amber-500/70 transition-colors">
                <div className="absolute inset-0 bg-gradient-to-br from-amber-950/40 to-transparent opacity-50 group-hover:opacity-80 transition-opacity" />
                <div className="relative z-10">
                  <p className="text-[10px] text-amber-400 uppercase mb-2">Market Value (MTX)</p>
                  <p className="text-3xl sm:text-4xl font-black text-amber-400 leading-none mb-3">${data.price}</p>
                  <div className="flex items-center gap-2">
                    <div className="w-2 h-2 bg-amber-500 rounded-full animate-pulse" />
                    <p className="text-[9px] text-amber-600 uppercase">Status: Volatile</p>
                  </div>
                </div>
              </div>
            </div>

            {/* 단백질 시퀀스 (Raw Data) - 개선된 디자인 */}
            <div className="relative bg-zinc-950/80 border-2 border-cyan-900/30 rounded-lg overflow-hidden group hover:border-cyan-500/50 transition-colors">
              {/* 헤더 태그 */}
              <div className="absolute top-0 right-0 bg-cyan-900/60 text-[8px] px-2 py-1 text-cyan-300 font-bold uppercase z-20 rounded-bl-lg border-l border-b border-cyan-500/30">
                RAW_DATA
              </div>
              
              <div className="p-4 pt-8">
                <p className="text-[10px] text-cyan-500 uppercase mb-3 flex items-center gap-2">
                  <span className="w-1 h-4 bg-cyan-500 rounded" />
                  PROTEIN_SEQUENCE_DUMP
                </p>
                
                {/* 스크롤 데이터 스트림 */}
                <div className="relative h-32 overflow-hidden rounded bg-black/50 border border-cyan-950/50 p-3">
                  <div className="text-[9px] sm:text-[10px] text-cyan-400/70 leading-tight break-all font-mono">
                    <div className="animate-text-scroll">
                      {Array(12).fill(data.proteinSeq).join(' · ')}
                    </div>
                  </div>
                  {/* 페이드 그라데이션 */}
                  <div className="absolute inset-0 bg-gradient-to-t from-black via-transparent to-transparent pointer-events-none" />
                  {/* 노이즈 텍스처 */}
                  <div className="absolute inset-0 opacity-10 bg-[url('data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48ZmlsdGVyIGlkPSJuIj48ZmVUdXJidWxlbmNlIHR5cGU9ImZyYWN0YWxOb2lzZSIgYmFzZUZyZXF1ZW5jeT0iLjciIG51bU9jdGF2ZXM9IjQiLz48L2ZpbHRlcj48cmVjdCB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgZmlsdGVyPSJ1cmwoI24pIiBvcGFjaXR5PSIuNSIvPjwvc3ZnPg==')] pointer-events-none" />
                </div>
              </div>
            </div>

            {/* 리셋 버튼 - 개선된 디자인 */}
            <button 
              onClick={() => setStatus('idle')}
              className="w-full mt-6 py-4 bg-zinc-950/50 border-2 border-zinc-700 rounded-lg text-zinc-400 text-xs sm:text-sm hover:bg-zinc-900 hover:border-emerald-600 hover:text-emerald-400 transition-all duration-300 uppercase tracking-wider font-bold active:scale-95 flex items-center justify-center gap-2"
            >
              <RefreshCw size={16} />
              <span>// RESET_SCANNER_MODULE</span>
            </button>
          </div>
        )}
      </div>

      {/* 커스텀 CSS 애니메이션 */}
      <style>{`
        /* 스캔라인 효과 */
        .scanlines::before {
          content: "";
          display: block;
          position: absolute;
          top: 0; left: 0; bottom: 0; right: 0;
          background: 
            linear-gradient(rgba(18, 16, 16, 0) 50%, rgba(0, 0, 0, 0.25) 50%), 
            linear-gradient(90deg, rgba(16, 185, 129, 0.03), rgba(6, 182, 212, 0.02), rgba(16, 185, 129, 0.03));
          background-size: 100% 2px, 3px 100%;
          pointer-events: none;
        }
        
        /* 애니메이션 */
        @keyframes scan-down {
          0% { top: 0; opacity: 1; }
          100% { top: 100%; opacity: 0.2; }
        }
        .animate-scan-down { 
          animation: scan-down 2s cubic-bezier(0.4, 0, 0.2, 1) infinite; 
        }

        @keyframes text-scroll {
          0% { transform: translateX(0); }
          100% { transform: translateX(-50%); }
        }
        .animate-text-scroll { 
          animation: text-scroll 25s linear infinite; 
        }

        @keyframes spin-slow {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
        .animate-spin-slow { 
          animation: spin-slow 8s linear infinite; 
        }

        @keyframes fade-in {
          from { opacity: 0; transform: translateY(10px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .animate-fade-in { 
          animation: fade-in 0.6s ease-out; 
        }
      `}</style>
    </div>
  );
}