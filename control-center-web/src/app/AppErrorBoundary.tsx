import { Component, createRef, type ErrorInfo, type ReactNode } from 'react';
import './app-recovery.css';

type AppErrorBoundaryProps = {
  children: ReactNode;
};

type AppErrorBoundaryState = {
  failed: boolean;
};

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { failed: false };
  private readonly retryRef = createRef<HTMLButtonElement>();

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('Control Center render failed', error, errorInfo);
  }

  componentDidMount(): void {
    if (this.state.failed) this.focusRecovery();
  }

  componentDidUpdate(
    _previousProps: AppErrorBoundaryProps,
    previousState: AppErrorBoundaryState,
  ): void {
    if (!previousState.failed && this.state.failed) {
      this.focusRecovery();
    }
  }

  private focusRecovery(): void {
    this.retryRef.current?.focus({ preventScroll: true });
  }

  render() {
    if (!this.state.failed) return this.props.children;

    return (
      <main className="app-recovery">
        <section
          aria-labelledby="app-recovery-title"
          className="app-recovery__surface"
          role="alert"
        >
          <span className="app-recovery__icon" aria-hidden="true">
            <svg fill="none" height="20" viewBox="0 0 24 24" width="20">
              <path d="M12 9v4m0 4h.01M10.3 4.4 2.7 18a2 2 0 0 0 1.75 3h15.1a2 2 0 0 0 1.75-3L13.7 4.4a2 2 0 0 0-3.4 0Z" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" />
            </svg>
          </span>
          <div className="app-recovery__copy">
            <h1 id="app-recovery-title">工作台需要重新载入</h1>
            <p>当前界面没有完整显示，你的数据未受影响。重新载入后可以继续。</p>
          </div>
          <div className="app-recovery__actions">
            <button
              className="ui-button"
              data-variant="primary"
              onClick={() => window.location.reload()}
              ref={this.retryRef}
              type="button"
            >
              重新载入
            </button>
          </div>
        </section>
      </main>
    );
  }
}
