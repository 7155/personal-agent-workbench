import { lazy, Suspense } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { queryClient } from '@/app/query-client';
import { ControlTransportProvider } from '@/app/control-transport';
import { ControlConnectionMonitor } from '@/app/control-connection-monitor';
import { GlobalFeedbackProvider } from '@/components/feedback';
import { ToastProvider, TooltipProvider } from '@/components/primitives';
import { MotionProvider } from '@/design/motion';
import { PawOsAppearanceProvider } from '@/design/paw-os-themes';
import { ThemeProvider } from '@/design/themes';
import { useFilePreviewStore } from '@/features/agent/file-preview/file-preview-store';
import { ProductIdentityProvider } from '@/features/identity/product-identity';
import { resolveFrontendProduct, type FrontendProduct } from './frontend-product';
import { standaloneSurfaceForPath } from './standalone-surface';
import '@/design/tokens.css';
import '@/design/typography.css';
import '@/design/workspace.css';
import '@/components/primitives/primitives.css';
import '@/components/primitives/showcase.css';
import '@/components/feedback/feedback.css';
import '@/components/layout/layout.css';

const FilePreviewHost = lazy(async () => ({
  default: (await import('@/features/agent/file-preview/FilePreviewHost')).FilePreviewHost,
}));

const PawOsApp = lazy(async () => ({
  default: (await import('@/paw-os/PawOsApp')).PawOsApp,
}));

const ScreenAssistant = lazy(async () => ({
  default: (await import('@/features/screen-assistant/ScreenAssistant')).ScreenAssistant,
}));

const LegacyProductApp = lazy(async () => ({
  default: (await import('./LegacyProductApp')).LegacyProductApp,
}));

const StandaloneEvolutionReportPage = lazy(async () => ({
  default: (await import('@/features/evolution-report/standalone')).StandaloneEvolutionReportPage,
}));

export function App({ frontendProduct }: { frontendProduct?: FrontendProduct } = {}) {
  const location = typeof window === 'undefined' ? { pathname: '/', search: '' } : window.location;
  const standaloneSurface = standaloneSurfaceForPath(
    location.pathname,
    location.search,
  );
  if (standaloneSurface === 'evolution-report') {
    return (
      <ThemeProvider forcedTheme="light">
        <PawOsAppearanceProvider>
          <MotionProvider>
            <Suspense fallback={<ProductLoading />}>
              <StandaloneEvolutionReportPage />
            </Suspense>
          </MotionProvider>
        </PawOsAppearanceProvider>
      </ThemeProvider>
    );
  }

  const product = frontendProduct ?? resolveFrontendProduct({
    configured: import.meta.env.VITE_PAW_FRONTEND,
    search: typeof window === 'undefined' ? '' : window.location.search,
  });

  return (
    <ThemeProvider>
      <PawOsAppearanceProvider>
        <MotionProvider>
        <TooltipProvider delayDuration={350}>
          <ToastProvider>
            <GlobalFeedbackProvider>
              <ControlTransportProvider>
                <ControlConnectionMonitor />
                <FilePreviewLayer />
                <QueryClientProvider client={queryClient}>
                  <ProductIdentityProvider>
                    <Suspense fallback={<ProductLoading />}>
                      {standaloneSurface === 'screen-assistant' || standaloneSurface === 'agent-capsule' ? <ScreenAssistant /> : product === 'paw-os' ? (
                        <PawOsApp />
                      ) : (
                        <LegacyProductApp />
                      )}
                    </Suspense>
                  </ProductIdentityProvider>
                </QueryClientProvider>
              </ControlTransportProvider>
            </GlobalFeedbackProvider>
          </ToastProvider>
        </TooltipProvider>
        </MotionProvider>
      </PawOsAppearanceProvider>
    </ThemeProvider>
  );
}

function ProductLoading() {
  // React replaces index.html's boot surface before the lazy product resolves.
  // Carry the same visible status across that handoff instead of an empty node.
  return <main aria-label="正在打开工作台" className="app-boot" role="status">
    <div className="app-boot__status">
      <span className="app-boot__indicator" aria-hidden="true" />
      <span className="app-boot__copy"><strong>正在准备工作台</strong><span>正在载入界面</span></span>
    </div>
  </main>;
}

function FilePreviewLayer() {
  const open = useFilePreviewStore((state) => state.open);
  if (!open) return null;
  return <Suspense fallback={null}><FilePreviewHost /></Suspense>;
}
