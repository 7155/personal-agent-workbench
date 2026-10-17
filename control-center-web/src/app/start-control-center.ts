type ControlCenterMount = {
  mountControlCenter: (root: HTMLElement) => void;
};

type StartControlCenterOptions = {
  load?: () => Promise<ControlCenterMount>;
  reload?: () => void;
};

const DEFAULT_LOAD_ERROR_MESSAGE = '工作台没有完整载入。你的数据没有改变，请检查连接后重新载入。';
const ENTRY_LOAD_TIMEOUT_MS = 30_000;

export async function startControlCenter({
  load = () => import('./mount-control-center'),
  reload = () => window.location.reload(),
}: StartControlCenterOptions = {}): Promise<void> {
  const root = document.getElementById('root');

  if (!root) {
    throw new Error('Missing #root mount point');
  }

  let loadTimeout: ReturnType<typeof setTimeout> | undefined;

  try {
    const deadline = new Promise<never>((_, reject) => {
      loadTimeout = setTimeout(() => reject(new Error('Control Center entry load timed out')), ENTRY_LOAD_TIMEOUT_MS);
    });
    const { mountControlCenter } = await Promise.race([load(), deadline]);
    mountControlCenter(root);
  } catch (error) {
    showStartupRecovery(root, reload);
    console.error('Control Center failed to start', error);
  } finally {
    clearTimeout(loadTimeout);
  }
}

function showStartupRecovery(root: HTMLElement, reload: () => void): void {
  const page = document.createElement('main');
  page.className = 'app-boot app-boot--failed';

  const surface = document.createElement('section');
  surface.className = 'app-boot__recovery';
  surface.setAttribute('role', 'alert');
  surface.setAttribute('aria-labelledby', 'app-boot-error-title');

  const copy = document.createElement('div');
  copy.className = 'app-boot__recovery-copy';

  const eyebrow = document.createElement('span');
  eyebrow.textContent = '启动未完成';

  const title = document.createElement('h1');
  title.id = 'app-boot-error-title';
  title.textContent = '工作台没有打开';

  const description = document.createElement('p');
  description.textContent = DEFAULT_LOAD_ERROR_MESSAGE;

  const retry = document.createElement('button');
  retry.className = 'app-boot__retry';
  retry.type = 'button';
  retry.textContent = '重新载入';
  retry.addEventListener('click', reload);

  copy.append(eyebrow, title, description);
  surface.append(copy, retry);
  page.append(surface);
  root.replaceChildren(page);
  retry.focus({ preventScroll: true });
}
