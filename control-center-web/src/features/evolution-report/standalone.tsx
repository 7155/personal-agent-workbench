import { ArrowLeft } from 'lucide-react';
import { useEffect } from 'react';
import { EvolutionReportFeature } from './index';

export function StandaloneEvolutionReportPage() {
  useEffect(() => {
    const previousTitle = document.title;
    document.title = '自我进化实验账本 · PAW';
    return () => { document.title = previousTitle; };
  }, []);

  return (
    <div className="evolution-report-standalone" data-testid="standalone-evolution-report">
      <header className="evolution-report-site-header">
        <a href="/?frontend=paw-os#/project-field">
          <ArrowLeft aria-hidden="true" size={15} />
          返回 PAW
        </a>
        <span>PAW · 实验报告</span>
        <small>固定证据快照</small>
      </header>
      <EvolutionReportFeature />
    </div>
  );
}
