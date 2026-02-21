// src/pages/TerminalScanner.tsx
import { useState, useEffect } from 'react';
import { useAutoAnimate } from '@formkit/auto-animate/react';
import { Scan, RefreshCw, AlertCircle, Radio, Crosshair, Cpu } from 'lucide-react';
import '../styles/TerminalScanner.css';

interface BioEconomicData {
  id: string;
  name: string;
  price: number;
  mutationRate: number;
  threat: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  llmAnalysis: string;
}

export default function TerminalScanner() {
  const [parent] = useAutoAnimate<HTMLDivElement>({ duration: 300, easing: 'ease-in-out' });
  const [status, setStatus] = useState<'idle' | 'scanning' | 'result'>('idle');
  const [data, setData] = useState<BioEconomicData | null>(null);

  // LLM 타이핑 이펙트용 상태
  const [displayedText, setDisplayedText] = useState('');

  const startScan = () => {
    setStatus('scanning');
    setDisplayedText('');

    // API 통신 시뮬레이션 (FastAPI Node B 호출)
    setTimeout(() => {
      const mockResult: BioEconomicData = {
        id: 'OBJ-2026-KNX-99',
        name: 'Contaminated Ration Pack',
        price: 15400.25,
        mutationRate: 89.4,
        threat: 'CRITICAL',
        llmAnalysis: "[LLM_NODE_B_ANALYSIS]\n해당 단백질 샘플은 2026년 1분기 초인플레이션 사태 당시 배급된 식량과 일치함.\nC_score 오염도 수식 분석 결과, 시장 경제 붕괴로 인한 스트레스 인자가 바이러스 변이를 89% 가속화시킴.\n\n권장 사항: 즉시 소각할 것. 섭취 시 48시간 내 감염 확정."
      };
      setData(mockResult);
      setStatus('result');
    }, 2500);
  };

  // LLM 텍스트 타이핑 이펙트 로직
  useEffect(() => {
    if (status === 'result' && data) {
      let i = 0;
      const interval = setInterval(() => {
        setDisplayedText(data.llmAnalysis.substring(0, i));
        i++;
        if (i > data.llmAnalysis.length) clearInterval(interval);
      }, 30);
      return () => clearInterval(interval);
    }
  }, [status, data]);

  return (
    <div className="scanner-container">
      {/* 상단 상태바 */}
      <div className="scanner-status-bar">
        <div className="status-bar-left">
          <span className="status-os">
            <Radio size={12} /> OS: BIO-ECON v26.2.20
          </span>
          <span className="status-link">LINK: LOCAL_NODE_B</span>
        </div>
        <div className="status-bar-right">
          <span className="status-battery">BAT: [|||||| ] 84%</span>
          <span className="status-alert">
            <AlertCircle size={12} /> ALERT: HAZARD ZONE
          </span>
        </div>
      </div>

      <div ref={parent} className="scanner-content-area">

        {/* 대기 및 스캔 화면 */}
        {(status === 'idle' || status === 'scanning') && (
          <div className="scanner-view-idle">
            <div className={`reticle-wrapper ${status === 'scanning' ? 'scanning' : ''}`}>
              <div className={`reticle-outer-ring ${status === 'scanning' ? 'scanning' : ''}`} />
              <div className={`reticle-inner-frame ${status === 'scanning' ? 'scanning' : ''}`}>
                <div className="reticle-crosshair-container">
                  <Crosshair size={48} className={`reticle-crosshair ${status === 'scanning' ? 'active' : ''}`} />
                </div>
                {status === 'scanning' && <div className="reticle-scan-area"><div className="scan-beam" /></div>}
              </div>
            </div>

            <p className="scanner-instruction">
              {status === 'scanning' ? '// ACQUIRING TARGET DATA...' : 'ALIGN SUBJECT WITHIN RETICLE'}
            </p>

            <button onClick={startScan} disabled={status === 'scanning'} className="scanner-btn">
              {status === 'scanning' ? <RefreshCw size={20} className="spin" /> : <Scan size={20} />}
              {status === 'scanning' ? 'SCANNING...' : 'INITIATE SCAN'}
            </button>
          </div>
        )}

        {/* 결과 화면 - LLM RAG 패널 포함 */}
        {status === 'result' && data && (
          <div className="result-dashboard">
            <div className="result-header">
              <p className="result-target-id">TARGET_ID: {data.id}</p>
              <h1 className="result-target-name">{data.name}</h1>
            </div>

            <div className={`result-threat-box ${data.threat === 'HIGH' || data.threat === 'CRITICAL' ? 'critical' : 'safe'}`}>
              <AlertCircle size={32} className="threat-icon" />
              <div>
                <p className="threat-label">Threat Assessment</p>
                <p className="threat-value">{data.threat}</p>
              </div>
            </div>

            <div className="result-stats-grid">
              <div className="stat-card mutation">
                <p className="stat-label">Mutation Rate</p>
                <p className="stat-value">{data.mutationRate}%</p>
              </div>
              <div className="stat-card market">
                <p className="stat-label">Market Value</p>
                <p className="stat-value">${data.price}</p>
              </div>
            </div>

            {/* LLM RAG 분석 창 */}
            <div className="llm-rag-terminal">
              <div className="llm-rag-header">
                <Cpu size={14} /> EDGE_NODE_B RAG ANALYSIS
              </div>
              <div className="llm-rag-content">
                {displayedText}
                <span className="llm-cursor"></span>
              </div>
            </div>

            <button onClick={() => setStatus('idle')} className="scanner-btn reset-btn">
              <RefreshCw size={16} /> ENGAGE NEW TARGET
            </button>
          </div>
        )}
      </div>
    </div>
  );
}