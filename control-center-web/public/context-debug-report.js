(() => {
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing || window.parent === window) return;
    event.preventDefault();
    window.parent.postMessage({ type: 'paw:context-report:close' }, '*');
  });
  const details = () => [...document.querySelectorAll('details')];
  document.getElementById('expand')?.addEventListener('click', () => {
    details().forEach((item) => { item.open = true; });
  });
  document.getElementById('collapse')?.addEventListener('click', () => {
    details().forEach((item) => { item.open = false; });
  });
  document.getElementById('search')?.addEventListener('input', (event) => {
    const query = event.target.value.trim().toLowerCase();
    document.querySelectorAll('.model-call').forEach((item) => {
      item.hidden = Boolean(query) && !item.dataset.search.includes(query);
    });
  });
  document.querySelectorAll('.report-controls button, .report-controls input').forEach((control) => { control.disabled = false; });
  document.querySelector('.report-controls')?.removeAttribute('aria-busy');
})();
